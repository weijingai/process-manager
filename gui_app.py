# -*- coding: utf-8 -*-
"""Windows进程管理工具（潍鲸 - weijing.co） - 原生桌面版（tkinter）

不依赖浏览器，所有界面与逻辑都内置在程序里：
  - 运行中服务 / 全部服务 / 进程 / 端口占用 四个视图
  - 服务的启动 / 停止 / 重启，进程与端口视图里也能直接停止/重启其承载的服务
  - 结束进程 / 结束进程树、修改启动类型、搜索、排序、自动刷新
  - 管理员权限提示与一键提权

用法：
    python gui_app.py
    python gui_app.py --smoke-test     # 无窗口自检：扫描一次后打印结果并退出
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import messagebox, ttk

if getattr(sys, "frozen", False):  # PyInstaller 打包后
    BASE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from core import APP_RELEASE_DATE, app_version, cleanup, control, monitor  # noqa: E402
from core import scanner, software, textutil  # noqa: E402

MAX_ROWS = 1000

# --------------------------------------------------------------------------- #
# 视觉主题（浅色 · 卡片化）
# --------------------------------------------------------------------------- #

FONT = "Microsoft YaHei"
BG = "#f5f5f7"          # 窗口底色（macOS 浅灰）
CARD = "#ffffff"        # 卡片底色
BORDER = "#e5e5ea"
BORDER_SOFT = "#ececf0"
TEXT = "#1d1d1f"
TEXT_STRONG = "#000000"
MUTED = "#86868b"
PRIMARY = "#007aff"
PRIMARY_DARK = "#0060df"
PRIMARY_DEEP = "#0058cc"       # 选中态：比常规主色更深，浅背景下更醒目
PRIMARY_SOFT = "#e5f0ff"
PRIMARY_SOFT_DEEP = "#d3e6ff"  # 选中卡片底色
ACTIVE_BG = "#dce9fb"          # 列表选中行底色
BORDER_STRONG = "#cfcfd6"      # 控件描边
GRAY_DEEP = "#e8e8ed"          # hover 底色
GREEN = "#34c759"
RED = "#e02d26"
ORANGE = "#ff9500"

#: 概览卡片标题 -> 顶部色条颜色
STAT_ACCENT = {
    "running": GREEN,
    "total": PRIMARY,
    "proc": "#5ac8fa",
    "cpu": ORANGE,
    "listen": "#af52de",
    "conn": "#8e8e93",
}

# --------------------------------------------------------------------------- #
# 视图定义
# --------------------------------------------------------------------------- #

# (字段, 标题, 宽度, 对齐)
TAB_COLUMNS = {
    "running": [
        ("display_name", "显示名称", 260, "w"),
        ("name", "服务名", 180, "w"),
        ("status_text", "状态", 80, "center"),
        ("pid", "PID", 70, "center"),
        ("process_name", "进程", 130, "w"),
        ("ports", "端口", 150, "w"),
        ("start_type_text", "启动类型", 80, "center"),
        ("memory_mb", "内存", 80, "e"),
    ],
    "services": [
        ("display_name", "显示名称", 250, "w"),
        ("name", "服务名", 170, "w"),
        ("status_text", "状态", 80, "center"),
        ("pid", "PID", 70, "center"),
        ("ports", "端口", 130, "w"),
        ("start_type_text", "启动类型", 80, "center"),
        ("username", "登录身份", 150, "w"),
    ],
    "processes": [
        ("name", "进程名", 180, "w"),
        ("pid", "PID", 70, "center"),
        ("ppid", "父 PID", 70, "center"),
        ("cpu", "CPU", 70, "e"),
        ("memory_mb", "内存", 80, "e"),
        ("ports", "端口", 140, "w"),
        ("create_time", "启动时间", 150, "w"),
        ("username", "用户", 120, "w"),
        ("exe", "路径 / 命令行", 420, "w"),
    ],
    "ports": [
        ("local_port", "端口", 70, "center"),
        ("proto", "协议", 70, "center"),
        ("local_ip", "本地地址", 190, "w"),
        ("state_text", "状态", 90, "center"),
        ("pid", "PID", 70, "center"),
        ("process_name", "进程", 150, "w"),
        ("remote_ip", "远程地址", 190, "w"),
    ],
}

# 首列操作列：点击单元格弹出与右键相同的操作菜单（便捷入口）
for _t in ("running", "services", "processes", "ports"):
    TAB_COLUMNS[_t].insert(0, ("__ops__", "操作", 100, "center"))

TAB_TITLES = {
    "running": "运行中服务",
    "services": "全部服务",
    "processes": "进程",
    "ports": "端口占用",
    "monitor": "系统监控",
    "cleanup": "磁盘清理",
    "search": "文件查找",
}

#: 页签在 Notebook 中的顺序，_on_tab_changed 靠索引反查页签名
TAB_ORDER = ("running", "services", "processes", "ports", "monitor", "cleanup", "search")

#: 内存/监控面板自动刷新的 ms 间隔
MONITOR_INTERVAL_MS = 2000

FILTERS = {
    "running": [("all", "全部状态"), ("running", "运行中"),
                ("start_pending", "启动中"), ("stop_pending", "停止中")],
    "services": [("all", "全部状态"), ("running", "运行中"),
                 ("stopped", "已停止"), ("disabled", "已禁用（启动类型）")],
    "processes": [("all", "全部进程"), ("with_port", "占用端口的进程"),
                  ("high_mem", "内存 > 100MB")],
    "ports": [("all", "全部连接"), ("listen", "仅监听端口"),
              ("tcp", "仅 TCP"), ("udp", "仅 UDP")],
}

DEFAULT_SORT = {
    "running": ("display_name", False),
    "services": ("display_name", False),
    "processes": ("memory_mb", True),
    "ports": ("local_port", False),
}

NUMERIC_KEYS = {"pid", "ppid", "cpu", "memory_mb", "local_port"}

#: 软件清理：来源下拉（中文显示值 -> 接口参数）
SOFT_SOURCE_MAP = {"全部来源": "all", "已安装软件": "installed", "AI 工具缓存": "agents"}
#: 软件清理：文件范围下拉
SOFT_SCOPE_MAP = {"仅缓存 / 日志": "cache", "全部文件": "all"}
#: 文件查找：类型下拉
FS_MODE_MAP = {"文件 + 文件夹": "all", "仅文件": "file", "仅文件夹": "dir"}


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def clip(value, limit: int = 0) -> str:
    """返回字段文本；limit > 0 时按字符长度截断（兼容旧调用，limit=0 表示不截断）。"""
    text = "" if value is None else str(value)
    if limit and len(text) > limit:
        return text[:limit - 1] + "…"
    return text


def fmt_cell(row: dict, key: str) -> str:
    if key == "__ops__":
        return "执行"
    v = row.get(key)
    if key == "ports":
        ports = v or []
        if not ports:
            return "-"
        text = ",".join(str(p) for p in ports[:8])
        return text + (f" +{len(ports) - 8}" if len(ports) > 8 else "")
    if key == "memory_mb":
        if v is None:
            return "-"
        return f"{v / 1024:.1f} GB" if v >= 1024 else f"{v} MB"
    if key == "cpu":
        return "-" if v is None else f"{v}%"
    if v is None or v == "":
        return "-"
    return clip(v)


# --------------------------------------------------------------------------- #
# 主程序
# --------------------------------------------------------------------------- #

class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.data = None
        self.busy = False
        self.tab = "running"
        self.sort = dict(DEFAULT_SORT)
        self.search_var = tk.StringVar()
        self.filter_var = tk.StringVar(value="all")
        self.auto_var = tk.BooleanVar(value=False)
        self.interval_var = tk.IntVar(value=10)
        self.trees: dict[str, ttk.Treeview] = {}
        self.rows: dict[str, dict] = {}          # tree iid -> 原始数据
        self._after_id = None
        self.tab_buttons: list[tk.Label] = []    # 自绘卡片页签
        self.hover_index = -1
        self.col_hidden: dict[str, set] = {}     # tab -> 被隐藏的列 key 集合
        self.visible_cols: dict[str, list] = {}  # tab -> 当前可见列 key 列表
        self.col_cards: dict[str, object] = {}    # tab -> 内联列显示卡片（显示列）
        self.col_card_open: dict[str, bool] = {}   # tab -> 列显示卡片是否已展开
        self.ops_labels: dict[str, list] = {}      # tab -> 首列「操作」覆盖的红色字体标签

        # --- 系统监控页签 ---
        self.mon_data = None
        self._mon_after_id = None
        self.mon_vars: dict[str, tk.StringVar] = {}
        self.core_canvas = None
        self.chart_canvas = None
        self.top_tree = None
        self.iface_tree = None
        self.disk_rows: list[tuple[tk.StringVar, tk.StringVar, ttk.Progressbar]] = []

        # --- 清理页签 ---
        self.clean_targets: list[dict] = []
        self.clean_files: list[dict] = []
        self.target_tree = None
        self.file_tree = None
        self.drive_var = tk.StringVar(value="")
        self.drive_values = [""]
        self.minmb_var = tk.IntVar(value=200)
        self.clean_opt_file_cache = tk.BooleanVar(value=False)
        self.clean_opt_standby = tk.BooleanVar(value=False)
        self.clean_busy = False

        try:
            monitor.start()
        except Exception:
            pass

        self.app_version = app_version()
        root.title(f"Windows进程管理工具（潍鲸 - weijing.co） v{self.app_version}")
        root.geometry("1320x840")
        root.minsize(1000, 640)
        self._configure_style()
        self._build_ui()
        self.root.after(150, lambda: self.refresh(force=True))

    # ---------------- 样式 ----------------

    def _configure_style(self) -> None:
        style = ttk.Style()
        # clam 是唯一能完整自定义配色的内置主题，其余主题会忽略 background 等选项
        try:
            style.theme_use("clam")
        except Exception:
            for name in ("vista", "xpnative", "default"):
                if name in style.theme_names():
                    try:
                        style.theme_use(name)
                        break
                    except Exception:
                        continue

        try:
            tkfont.nametofont("TkDefaultFont").configure(family=FONT, size=9)
        except Exception:
            pass
        self.root.configure(background=BG)

        # ---- 容器 / 文本 ----
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT_STRONG, font=(FONT, 9))
        style.configure("Card.TLabel", background=CARD, foreground=TEXT, font=(FONT, 9))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=(FONT, 9))
        style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=(FONT, 9))
        style.configure("Head.TLabel", background=BG, foreground=TEXT_STRONG,
                        font=(FONT, 10, "bold"))
        style.configure("CardHead.TLabel", background=CARD, foreground=TEXT_STRONG,
                        font=(FONT, 10, "bold"))
        # 嵌在卡片内部的表格：去掉自身边框，避免与卡片边框重影
        style.configure("Plain.Treeview", borderwidth=0, relief="flat")
        style.configure("Stat.TLabel", background=CARD, foreground=TEXT_STRONG,
                        font=(FONT, 16, "bold"))

        # ---- 分组框 ----
        style.configure("TLabelframe", background=CARD, bordercolor=BORDER_STRONG,
                        relief="flat")
        style.configure("TLabelframe.Label", background=CARD, foreground=TEXT_STRONG,
                        font=(FONT, 10, "bold"))

        # ---- 按钮 ----
        # 按钮：描边更深、hover / pressed 逐层加深，选中态（primary）用深主色
        # 可操作按钮：背景加深的浅蓝灰实心填充，描边与 hover / pressed 逐层加深
        style.configure("TButton", background="#e9eef7", foreground=TEXT_STRONG,
                        bordercolor=BORDER_STRONG, lightcolor="#e9eef7",
                        darkcolor="#c8d2e2", relief="flat",
                        padding=(13, 7), font=(FONT, 9, "bold"))
        style.map("TButton",
                  background=[("active", "#dde5f1"), ("pressed", "#ccd6e6"),
                              ("disabled", CARD)],
                  bordercolor=[("active", "#a7a9b5")],
                  foreground=[("disabled", "#9ca3af")])
        # 次要按钮：更浅，用于纯辅助操作
        style.configure("Ghost.TButton", background="#f3f4f8", foreground=TEXT_STRONG,
                        bordercolor="#c2c4cd", lightcolor="#f3f4f8", darkcolor="#d6d8e0",
                        relief="flat", padding=(13, 7), font=(FONT, 9, "bold"))
        style.map("Ghost.TButton",
                  background=[("active", GRAY_DEEP), ("pressed", "#d6d8e0"),
                              ("disabled", CARD)],
                  bordercolor=[("active", "#a7a9b5")])
        style.configure("Primary.TButton", background=PRIMARY_DARK, foreground="#ffffff",
                        bordercolor=PRIMARY_DARK, lightcolor=PRIMARY_DARK,
                        darkcolor=PRIMARY_DEEP, relief="flat", padding=(16, 8),
                        font=(FONT, 9, "bold"))
        style.map("Primary.TButton",
                  background=[("active", PRIMARY_DEEP), ("pressed", PRIMARY_DEEP)],
                  bordercolor=[("active", PRIMARY_DEEP)])
        style.configure("Danger.TButton", background=RED, foreground="#ffffff",
                        bordercolor=RED, lightcolor=RED, darkcolor="#b91c1c",
                        relief="flat", padding=(15, 8), font=(FONT, 9, "bold"))
        style.map("Danger.TButton",
                  background=[("active", "#b91c1c"), ("pressed", "#b91c1c")],
                  bordercolor=[("active", "#b91c1c")])

        # ---- 表格 ----
        style.configure("Treeview", background=CARD, fieldbackground=CARD,
                        bordercolor=BORDER, borderwidth=1, relief="flat",
                        rowheight=30, font=(FONT, 9))
        style.configure("Treeview.Heading", background="#eff2f7", foreground=TEXT_STRONG,
                        bordercolor=BORDER_STRONG, relief="flat", padding=(8, 6),
                        font=(FONT, 9, "bold"))
        style.map("Treeview.Heading", background=[("active", "#e2e6ee")])
        style.map("Treeview",
                  background=[("selected", ACTIVE_BG)],
                  foreground=[("selected", PRIMARY_DEEP)])

        # ---- 输入 / 选择 ----
        for name in ("TEntry", "TCombobox", "TSpinbox"):
            style.configure(name, fieldbackground=CARD, background=CARD,
                            foreground=TEXT_STRONG, bordercolor=BORDER_STRONG,
                            relief="flat", arrowcolor=TEXT, padding=(7, 5))
            style.map(name, bordercolor=[("focus", PRIMARY_DEEP)],
                      arrowcolor=[("disabled", "#9ca3af")])
        style.configure("TCheckbutton", background=BG, foreground=TEXT_STRONG,
                        font=(FONT, 9), indicatorcolor=PRIMARY_DEEP,
                        indicatorbackground=CARD)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("Card.TCheckbutton", background=CARD, foreground=TEXT_STRONG,
                        font=(FONT, 9), indicatorcolor=PRIMARY_DEEP,
                        indicatorbackground=CARD)
        style.map("Card.TCheckbutton", background=[("active", CARD)])

        # ---- 进度条 / 滚动条 / 分隔线 ----
        style.configure("TProgressbar", background=PRIMARY, troughcolor=BORDER_SOFT,
                        bordercolor=BORDER_SOFT, lightcolor=PRIMARY, darkcolor=PRIMARY,
                        thickness=9)
        style.configure("Vertical.TScrollbar", background="#d1d1d6", troughcolor="#f5f5f7",
                        bordercolor=BORDER_SOFT, arrowcolor=MUTED, relief="flat")
        style.configure("Horizontal.TScrollbar", background="#d1d1d6", troughcolor="#f5f5f7",
                        bordercolor=BORDER_SOFT, arrowcolor=MUTED, relief="flat")
        style.map("Vertical.TScrollbar", background=[("active", "#cbd5e1")])
        style.map("Horizontal.TScrollbar", background=[("active", "#cbd5e1")])
        style.configure("TSeparator", background=BORDER)

        # ---- 页签：改用自绘卡片按钮，隐藏 Notebook 原生页签条 ----
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=[0, 0, 0, 0])
        try:
            style.layout("TNotebook.Tab", [])
        except Exception:
            pass

    # ---------------- 界面搭建 ----------------

    # --------- 通用：白色卡片容器 ---------

    @staticmethod
    def _card(parent, **kw) -> tk.Frame:
        """带 1px 边框的白色卡片容器（tk.Frame 才能自由定义边框与底色）。"""
        return tk.Frame(parent, bg=CARD, highlightthickness=1,
                        highlightbackground=BORDER, **kw)

    def _build_ui(self) -> None:
        # ---------- 顶部标题栏 ----------
        header = self._card(self.root)
        header.pack(fill="x")
        top = tk.Frame(header, bg=CARD)
        top.pack(fill="x", padx=16, pady=11)

        # macOS 窗口红绿灯（装饰）
        traffic = tk.Frame(top, bg=CARD)
        for _c in ("#ff5f57", "#febc2e", "#28c840"):
            _cv = tk.Canvas(traffic, width=12, height=12, bg=CARD, highlightthickness=0)
            _cv.create_oval(1, 1, 11, 11, fill=_c, outline="")
            _cv.pack(side="left", padx=3)
        traffic.pack(side="left", padx=(2, 8))

        tk.Label(top, text="WM", bg=PRIMARY, fg="#ffffff", font=(FONT, 13, "bold"),
                 padx=13, pady=7).pack(side="left")
        brand = tk.Frame(top, bg=CARD)
        brand.pack(side="left", padx=13)
        tk.Label(brand, text="Windows进程管理工具（潍鲸 - weijing.co）", bg=CARD, fg=TEXT_STRONG,
                 font=(FONT, 14, "bold")).pack(anchor="w")
        tk.Label(brand, text=f"服务 · 进程 · 端口 · 系统监控 · 磁盘清理 · 文件查找        "
                             f"版本 v{app_version()}（{APP_RELEASE_DATE}）",
                 bg=CARD, fg=MUTED, font=(FONT, 9)).pack(anchor="w")

        ttk.Button(top, text="刷新扫描", style="Primary.TButton",
                   command=lambda: self.refresh(force=True)).pack(side="right")
        ttk.Checkbutton(top, text="自动刷新", style="Card.TCheckbutton",
                        variable=self.auto_var,
                        command=self._apply_auto).pack(side="right", padx=(0, 12))
        ttk.Label(top, text="秒", style="CardMuted.TLabel").pack(side="right", padx=(0, 4))
        ttk.Spinbox(top, from_=5, to=300, width=4, textvariable=self.interval_var,
                    command=self._apply_auto).pack(side="right", padx=(0, 4))
        self.admin_label = ttk.Label(top, text="", style="Card.TLabel")
        self.admin_label.pack(side="right", padx=(0, 14))
        self.elevate_btn = ttk.Button(top, text="申请管理员权限", command=self.do_elevate)
        self.elevate_btn.pack(side="right")

        # ---------- 概览卡片 ----------
        stats = tk.Frame(self.root, bg=BG)
        stats.pack(fill="x", padx=8, pady=(13, 0))
        self.stat_vars = {}
        for key, title in (("running", "运行中服务"), ("total", "服务总数"),
                           ("proc", "进程数"), ("cpu", "CPU / 内存"),
                           ("listen", "监听端口"), ("conn", "连接总数")):
            card = self._card(stats)
            card.pack(side="left", fill="x", expand=True, padx=7, pady=4)
            tk.Frame(card, bg=STAT_ACCENT.get(key, PRIMARY), height=3).pack(fill="x")
            ttk.Label(card, text=title, style="CardMuted.TLabel").pack(
                anchor="w", padx=14, pady=(9, 0))
            var = tk.StringVar(value="-")
            ttk.Label(card, textvariable=var, style="Stat.TLabel").pack(
                anchor="w", padx=14, pady=(1, 10))
            self.stat_vars[key] = var

        # ---------- 页签（卡片按钮） ----------
        tabbar = tk.Frame(self.root, bg=BG)
        tabbar.pack(fill="x", padx=14, pady=(12, 0))
        self._build_tabbar(tabbar)

        # ---------- 内容区 ----------
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=14, pady=(11, 0))
        builders = {
            "monitor": self._build_monitor_tab,
            "cleanup": self._build_cleanup_tab,
            "search": self._build_search_tab,
        }
        for key in TAB_ORDER:
            frame = ttk.Frame(self.notebook, padding=(8, 6))
            self.notebook.add(frame, text=TAB_TITLES[key])
            if key in builders:
                builders[key](frame)
            else:
                self._build_tab(frame, key)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # ---------- 底栏：仅保留状态与进度（备注信息已按需求移除） ----------
        bottom = self._card(self.root)
        bottom.pack(fill="x", padx=14, pady=(11, 14))
        foot = tk.Frame(bottom, bg=CARD)
        foot.pack(fill="x", padx=14, pady=6)
        self.status_var = tk.StringVar(value="准备就绪")
        ttk.Label(foot, textvariable=self.status_var,
                  style="CardMuted.TLabel").pack(side="right", padx=(0, 12))
        tk.Label(foot, text=f"v{app_version()}", bg=CARD, fg=PRIMARY_DEEP,
                 font=(FONT, 9, "bold")).pack(side="right", padx=(0, 10))
        self.progress = ttk.Progressbar(foot, mode="indeterminate", length=150)
        self.progress.pack(side="right")

        self._paint_tabs()

    # ---------------- 卡片式页签 ----------------

    def _build_tabbar(self, parent: tk.Frame) -> None:
        self.tab_buttons: list[tk.Label] = []
        self.hover_index = -1
        for idx, key in enumerate(TAB_ORDER):
            btn = tk.Label(parent, text=TAB_TITLES[key], bg=CARD, fg="#3c3c43",
                           font=(FONT, 11, "bold"), padx=22, pady=10,
                           highlightthickness=1, highlightbackground=BORDER_STRONG,
                           highlightcolor=BORDER_STRONG, cursor="hand2")
            btn.pack(side="left", padx=(0, 12))
            btn.bind("<Button-1>", lambda _e, i=idx: self.select_tab(i))
            btn.bind("<Enter>", lambda _e, i=idx: self._hover_tab(i, True))
            btn.bind("<Leave>", lambda _e, i=idx: self._hover_tab(i, False))
            self.tab_buttons.append(btn)

    def _hover_tab(self, idx: int, inside: bool) -> None:
        self.hover_index = idx if inside else -1
        self._paint_tabs()

    def _paint_tabs(self) -> None:
        cur = TAB_ORDER.index(self.tab) if self.tab in TAB_ORDER else 0
        for i, btn in enumerate(getattr(self, "tab_buttons", [])):
            # 当前页签：白底 + 深蓝粗体 + 实心描边；未选：浅灰字，hover 加深
            if i == cur:
                btn.configure(bg="#ffffff", fg=PRIMARY_DEEP,
                              font=(FONT, 11, "bold"),
                              highlightbackground=PRIMARY_DARK,
                              highlightcolor=PRIMARY_DARK,
                              highlightthickness=2)
            elif i == self.hover_index:
                btn.configure(bg="#eef1f7", fg=TEXT_STRONG,
                              font=(FONT, 11, "bold"),
                              highlightbackground="#b9b9c2",
                              highlightcolor="#b9b9c2",
                              highlightthickness=1)
            else:
                btn.configure(bg=CARD, fg="#3c3c43",
                              font=(FONT, 11, "bold"),
                              highlightbackground=BORDER_STRONG,
                              highlightcolor=BORDER_STRONG,
                              highlightthickness=1)

    def select_tab(self, idx: int) -> None:
        try:
            self.notebook.select(idx)
        except Exception:
            pass
        self._paint_tabs()

    def _build_tab(self, frame: ttk.Frame, key: str) -> None:
        tools = self._card(frame)
        tools.pack(fill="x", pady=(0, 10))
        row = tk.Frame(tools, bg=CARD)
        row.pack(fill="x", padx=12, pady=9)
        ttk.Label(row, text="搜索：", style="CardMuted.TLabel").pack(side="left")
        entry = ttk.Entry(row, textvariable=self.search_var, width=34)
        entry.pack(side="left", padx=(0, 8))
        entry.bind("<Return>", lambda e: self._render())
        ttk.Button(row, text="清除", width=6,
                   command=lambda: (self.search_var.set(""), self._render())).pack(side="left")
        ttk.Label(row, text="筛选：", style="CardMuted.TLabel").pack(side="left", padx=(18, 0))
        combo = ttk.Combobox(row, state="readonly", width=18, textvariable=self.filter_var)
        combo["values"] = [t for _, t in FILTERS[key]]
        combo.current(0)
        combo.bind("<<ComboboxSelected>>", lambda e: self._render())
        self.filter_combo = combo
        combo.pack(side="left", padx=(0, 8))
        col_btn = ttk.Button(row, text="显示列",
                            command=lambda k=key: self.toggle_col_card(k))
        col_btn.pack(side="left", padx=(12, 0))
        # 列显示卡片：点击「显示列」展开/收起的内联卡片（非弹窗）
        col_card = tk.Frame(tools, bg=CARD, relief="solid", bd=1,
                            highlightbackground=BORDER_SOFT, highlightthickness=1)
        self.col_cards[key] = col_card
        self.col_card_open[key] = False
        inner = tk.Frame(col_card, bg=CARD)
        inner.pack(fill="x", padx=12, pady=8)
        ttk.Label(inner, text="勾选要显示的列：", style="CardMuted.TLabel").pack(
            anchor="w", pady=(0, 6))
        _hidden = self.col_hidden.setdefault(key, set())
        for c in TAB_COLUMNS[key]:
            cid, title, width, _a = c
            if cid == "__ops__":
                continue  # 操作列固定显示，不进「显示列」勾选
            chip = tk.Frame(inner, bg=CARD, highlightthickness=1,
                            highlightbackground=BORDER_SOFT)
            chip.pack(anchor="w", padx=2, pady=3, fill="x")
            var = tk.BooleanVar(value=cid not in _hidden)
            ttk.Checkbutton(chip, text=title, variable=var,
                            command=lambda cid=cid, var=var: self._toggle_col(key, cid, var.get()),
                            style="Card.TCheckbutton").pack(anchor="w", padx=10, pady=7)
        col_card.pack_forget()  # 默认收起
        count_var = tk.StringVar(value="共 0 条")
        ttk.Label(row, textvariable=count_var, style="CardMuted.TLabel").pack(side="right")
        self.count_var = count_var

        wrap = tk.Frame(frame, bg=BG)
        wrap.pack(fill="both", expand=True)
        self._create_tree(wrap, key)

    def _create_tree(self, wrap: tk.Frame, key: str) -> None:
        cols = [c for c in TAB_COLUMNS[key] if c[0] not in self.col_hidden.get(key, set())]
        self.visible_cols[key] = [c[0] for c in cols]
        tree = ttk.Treeview(wrap, columns=[c[0] for c in cols], show="headings",
                           selectmode="browse")
        for c in cols:
            cid, title, width, anchor = c
            if cid == "__ops__":
                tree.heading(cid, text=title)  # 操作列不参与排序
            else:
                tree.heading(cid, text=title, command=lambda c=cid: self._sort_by(c))
            tree.column(cid, width=width, anchor=anchor,
                        stretch=(cid in ("display_name", "exe", "name")))
        ybar = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        xbar = ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview)
        self.ops_labels[key] = []

        def _yscroll(*a):
            ybar.set(*a)
            self.root.after_idle(lambda: self._layout_ops_labels(key))

        def _xscroll(*a):
            xbar.set(*a)
            self.root.after_idle(lambda: self._layout_ops_labels(key))

        tree.configure(yscrollcommand=_yscroll, xscrollcommand=_xscroll)
        tree.bind("<Configure>", lambda _e: self._layout_ops_labels(key))
        tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        tree.tag_configure("running", foreground="#15803d")
        tree.tag_configure("stopped", foreground="#6b7280")
        tree.tag_configure("pending", foreground="#c2410c")
        tree.tag_configure("striped", background="#fafbfe")
        tree.bind("<Double-1>", lambda e: self.show_detail())
        tree.bind("<Button-3>", self._popup_menu)
        tree.bind("<Button-1>", self._on_tree_click)
        self.trees[key] = tree

    def _rebuild_columns(self, tab: str) -> None:
        """根据 col_hidden 重建某页签表格的列（显示 / 隐藏）。"""
        tree = self.trees.get(tab)
        if tree is None:
            return
        cols = [c for c in TAB_COLUMNS[tab] if c[0] not in self.col_hidden.get(tab, set())]
        self.visible_cols[tab] = [c[0] for c in cols]
        tree["columns"] = tuple(c[0] for c in cols)
        for c in cols:
            cid, title, width, anchor = c
            if cid == "__ops__":
                tree.heading(cid, text=title)  # 操作列不参与排序
            else:
                tree.heading(cid, text=title, command=lambda c=cid: self._sort_by(c))
            tree.column(cid, width=width, anchor=anchor,
                        stretch=(cid in ("display_name", "exe", "name")))
        self._render()

    def _layout_ops_labels(self, key: str) -> None:
        """在首列「操作」单元格上覆盖红色字体标签（仅当前页签、可见行），点击弹出操作菜单。"""
        tree = self.trees.get(key)
        if tree is None:
            return
        for b in self.ops_labels.get(key, []):
            try:
                b.destroy()
            except Exception:
                pass
        self.ops_labels[key] = []
        if self.tab != key:
            return
        wrap = tree.master
        try:
            offx = tree.winfo_rootx() - wrap.winfo_rootx()
            offy = tree.winfo_rooty() - wrap.winfo_rooty()
        except Exception:
            offx = offy = 0
        for iid in tree.get_children():
            try:
                bx = tree.bbox(iid, "__ops__")
            except Exception:
                bx = None
            if not bx:
                continue
            lbl = tk.Label(wrap, text="执行", fg=RED, bg=CARD,
                           font=(FONT, 9, "bold"), cursor="hand2")
            lbl._iid = iid
            lbl.bind("<Button-1>", lambda _e, l=lbl, i=iid: self._open_ops_at(l, i))
            lbl.place(x=offx + bx[0] + 6,
                      y=offy + bx[1] + (bx[3] - 20) // 2,
                      width=max(36, bx[2] - 12), height=20)
            self.ops_labels[key].append(lbl)

    def _open_ops_at(self, btn, iid: str) -> None:
        """点击首列「操作」红色字体标签：弹出与右键相同的操作菜单。"""
        tree = self.trees.get(self.tab)
        if tree is None:
            return
        tree.selection_set(iid)
        row = self.rows.get(iid)
        menu = self._build_context_menu(row)
        try:
            rx = btn.winfo_rootx() + 2
            ry = btn.winfo_rooty() + btn.winfo_height() + 2
        except Exception:
            rx = ry = 0
        menu.tk_popup(rx, ry)
        self._last_menu = menu

    def toggle_col_card(self, tab: str) -> None:
        """「显示列」按钮：展开/收起内联的列显示卡片（非弹窗）。"""
        card = self.col_cards.get(tab)
        if card is None:
            return
        if self.col_card_open.get(tab):
            card.pack_forget()
            self.col_card_open[tab] = False
        else:
            card.pack(fill="x", pady=(8, 0))
            self.col_card_open[tab] = True

    def _toggle_col(self, tab: str, cid: str, visible: bool) -> None:
        hidden = self.col_hidden.setdefault(tab, set())
        if visible:
            hidden.discard(cid)
        else:
            hidden.add(cid)
        self._rebuild_columns(tab)

    def _build_context_menu(self, row) -> tk.Menu:
        """按选中行类型（服务 / 进程）生成右键菜单，含启停管理的确认提示。"""
        menu = tk.Menu(self.root, tearoff=0, font=(FONT, 9), bg=CARD, fg=TEXT,
                       activebackground=PRIMARY_SOFT, activeforeground=PRIMARY,
                       relief="flat", bd=1)
        is_service = row is not None and ("start_type" in row or row.get("kind") == "service")
        if is_service:
            name = row.get("display_name") or row.get("name") or "该服务"
            menu.add_command(label=f"启动服务（{name}）",
                             command=lambda: self.service_action("start"))
            menu.add_command(label=f"停止服务（{name}）",
                             command=lambda: self.service_action("stop"))
            menu.add_command(label=f"重启服务（{name}）",
                             command=lambda: self.service_action("restart"))
            menu.add_separator()
            menu.add_command(label="设为自动启动", command=lambda: self.set_start_type("auto"))
            menu.add_command(label="设为手动启动", command=lambda: self.set_start_type("manual"))
            menu.add_command(label="禁用服务", command=lambda: self.set_start_type("disabled"))
        else:
            pname = (row or {}).get("name") or (row or {}).get("process_name") or "该进程"
            menu.add_command(label=f"结束进程（{pname}）", command=lambda: self.kill(False))
            menu.add_command(label=f"结束进程树（{pname}）", command=lambda: self.kill(True))
        menu.add_separator()
        menu.add_command(label="查看详情", command=self.show_detail)
        return menu

    def _popup_menu(self, event) -> None:
        tree = self.trees.get(self.tab)
        if tree is None:
            return
        iid = tree.identify_row(event.y)
        if not iid:
            return
        tree.selection_set(iid)
        row = self._selected()
        menu = self._build_context_menu(row)
        menu.tk_popup(event.x_root, event.y_root)
        # 保留引用，防止菜单对象被提前回收
        self._last_menu = menu

    def _on_tree_click(self, event) -> None:
        """点击首列「操作」列：在鼠标位置弹出与右键相同的操作菜单。"""
        tree = self.trees.get(self.tab)
        if tree is None:
            return
        if tree.identify("region", event.x, event.y) != "cell":
            return
        col = tree.identify_column(event.x)
        try:
            idx = int(col.replace("#", "")) - 1
        except ValueError:
            return
        vis = self.visible_cols.get(self.tab) or []
        if idx < 0 or idx >= len(vis) or vis[idx] != "__ops__":
            return
        iid = tree.identify_row(event.y)
        if not iid:
            return
        tree.selection_set(iid)
        row = self._selected()
        menu = self._build_context_menu(row)
        menu.tk_popup(event.x_root, event.y_root)
        self._last_menu = menu

    def _make_treenav(self, parent, root_title, definitions):
        """在 parent 内创建树状导航：左侧树（与主界面同色调）+ 右侧内容面板。

        definitions: [(key, title, build_fn)]；点击树的子节点切换右侧内容，
        树根为模块名（如「系统监控」「磁盘清理」），子节点为内部项目。
        """
        style = ttk.Style()
        style.configure("Nav.Treeview", background=CARD, foreground=TEXT,
                        fieldbackground=CARD, borderwidth=0, relief="flat",
                        font=(FONT, 10), rowheight=30)
        style.map("Nav.Treeview",
                  background=[("selected", ACTIVE_BG)],
                  foreground=[("selected", PRIMARY_DEEP)])

        body = tk.Frame(parent, bg=BG)
        body.pack(fill="both", expand=True)

        side = self._card(body)
        side.pack(side="left", fill="y", padx=(0, 10))
        tree = ttk.Treeview(side, show="tree", selectmode="browse",
                            style="Nav.Treeview", height=10)
        tree.column("#0", width=150, stretch=False)
        tree.pack(fill="both", expand=True, padx=6, pady=6)
        tree.tag_configure("navroot", font=(FONT, 10, "bold"), foreground=TEXT_STRONG)

        root_iid = tree.insert("", "end", text=root_title, open=True, tags=("navroot",))
        frames = {}
        iid_map = {}
        for key, title, build_fn in definitions:
            f = tk.Frame(body, bg=BG)
            frames[key] = f
            iid_map[tree.insert(root_iid, "end", text=title)] = key
            build_fn(f)

        first_key = definitions[0][0]
        frames[first_key].pack(fill="both", expand=True)
        kids = tree.get_children(root_iid)
        if kids:
            tree.selection_set(kids[0])

        def on_select(_e=None):
            sel = tree.selection()
            if not sel:
                return
            key = iid_map.get(sel[0])
            if key is None:  # 点中根节点：回到第一项
                kids = tree.get_children(root_iid)
                if kids:
                    tree.selection_set(kids[0])
                return
            for k, f in frames.items():
                if k == key:
                    f.pack(fill="both", expand=True)
                else:
                    f.pack_forget()

        tree.bind("<<TreeviewSelect>>", on_select)

    # ---------------- 系统监控页签 ----------------

    def _build_monitor_tab(self, frame: ttk.Frame) -> None:

        def build_overview(f):
            accent = {"cpu": PRIMARY, "mem": ORANGE, "disk": GREEN, "net": "#0ea5e9"}
            items = (("cpu", "CPU 使用率"), ("mem", "内存使用率"),
                     ("disk", "磁盘 IO"), ("net", "网络速率"))
            cards = tk.Frame(f, bg=BG)
            cards.pack(fill="x")
            for i, (key, title) in enumerate(items):
                card = self._card(cards)
                card.pack(side="left", fill="x", expand=True,
                          padx=(0, 12) if i < len(items) - 1 else (0, 0))
                tk.Frame(card, bg=accent[key], height=3).pack(fill="x")
                ttk.Label(card, text=title, style="CardMuted.TLabel").pack(
                    anchor="w", padx=14, pady=(9, 0))
                var = tk.StringVar(value="-")
                ttk.Label(card, textvariable=var, style="Stat.TLabel").pack(
                    anchor="w", padx=14, pady=(1, 0))
                foot = tk.StringVar(value="-")
                ttk.Label(card, textvariable=foot, style="CardMuted.TLabel").pack(
                    anchor="w", padx=14, pady=(0, 10))
                self.mon_vars[key] = var
                self.mon_vars[key + "_foot"] = foot
            chart_card = self._card(f)
            chart_card.pack(fill="x", pady=(12, 0))
            head = tk.Frame(chart_card, bg=CARD)
            head.pack(fill="x", padx=14, pady=(11, 0))
            ttk.Label(head, text="CPU / 内存趋势", style="CardHead.TLabel").pack(side="left")
            self.chart_range = tk.StringVar(value="最近 5 分钟")
            ttk.Label(head, textvariable=self.chart_range,
                      style="CardMuted.TLabel").pack(side="left", padx=10)
            legend = tk.Frame(head, bg=CARD)
            legend.pack(side="right")
            ttk.Label(legend, text="■ CPU %", style="Card.TLabel",
                      foreground=PRIMARY).pack(side="left", padx=8)
            ttk.Label(legend, text="■ 内存 %", style="Card.TLabel",
                      foreground=ORANGE).pack(side="left")
            self.chart_canvas = tk.Canvas(chart_card, height=170, bg="#fbfcfe",
                                          highlightthickness=1, highlightbackground=BORDER_SOFT)
            self.chart_canvas.pack(fill="x", padx=14, pady=(8, 12))
            self.chart_canvas.bind("<Configure>", lambda e: self._draw_chart())

        def build_cores(f):
            card = self._card(f)
            card.pack(fill="both", expand=True)
            lh = tk.Frame(card, bg=CARD)
            lh.pack(fill="x", padx=14, pady=(11, 0))
            ttk.Label(lh, text="逻辑核心占用", style="CardHead.TLabel").pack(side="left")
            self.core_canvas = tk.Canvas(card, height=76, bg="#fbfcfe",
                                         highlightthickness=1, highlightbackground=BORDER_SOFT)
            self.core_canvas.pack(fill="x", padx=14, pady=(8, 12))
            self.core_canvas.bind("<Configure>", lambda e: self._draw_cores())

        def build_disk(f):
            card = self._card(f)
            card.pack(fill="both", expand=True)
            rh = tk.Frame(card, bg=CARD)
            rh.pack(fill="x", padx=14, pady=(11, 0))
            ttk.Label(rh, text="磁盘分区", style="CardHead.TLabel").pack(side="left")
            self.disk_box = tk.Frame(card, bg=CARD)
            self.disk_box.pack(fill="x", padx=14, pady=(8, 12))

        def build_top(f):
            card = self._card(f)
            card.pack(fill="both", expand=True)
            th = tk.Frame(card, bg=CARD)
            th.pack(fill="x", padx=14, pady=(11, 0))
            ttk.Label(th, text="资源占用 Top", style="CardHead.TLabel").pack(side="left")
            top_cols = ("name", "pid", "cpu", "memory")
            self.top_tree = ttk.Treeview(card, columns=top_cols, show="headings",
                                         height=8, style="Plain.Treeview")
            for cid, title, width, anchor in (("name", "进程名", 220, "w"),
                                              ("pid", "PID", 80, "center"),
                                              ("cpu", "CPU %", 90, "e"),
                                              ("memory", "内存", 110, "e")):
                self.top_tree.heading(cid, text=title)
                self.top_tree.column(cid, width=width, anchor=anchor,
                                     stretch=(cid == "name"))
            self.top_tree.tag_configure("striped", background="#fafbfe")
            self.top_tree.pack(fill="x", padx=14, pady=(8, 12))

        def build_net(f):
            card = self._card(f)
            card.pack(fill="both", expand=True)
            ih = tk.Frame(card, bg=CARD)
            ih.pack(fill="x", padx=14, pady=(11, 0))
            ttk.Label(ih, text="网络适配器", style="CardHead.TLabel").pack(side="left")
            if_cols = ("name", "state", "ip", "speed", "io")
            self.iface_tree = ttk.Treeview(card, columns=if_cols, show="headings",
                                           height=5, style="Plain.Treeview")
            for cid, title, width, anchor in (("name", "网卡", 200, "w"),
                                              ("state", "状态", 80, "center"),
                                              ("ip", "IPv4 地址", 150, "w"),
                                              ("speed", "速率", 100, "center"),
                                              ("io", "累计流量 ↓/↑", 260, "w")):
                self.iface_tree.heading(cid, text=title)
                self.iface_tree.column(cid, width=width, anchor=anchor,
                                       stretch=(cid in ("io", "name")))
            self.iface_tree.tag_configure("striped", background="#fafbfe")
            self.iface_tree.pack(fill="x", padx=14, pady=(8, 12))

        self._make_treenav(frame, "系统监控", [
            ("overview", "概览", build_overview),
            ("cores", "核心占用", build_cores),
            ("disk", "磁盘分区", build_disk),
            ("top", "资源 TOP", build_top),
            ("net", "网络适配器", build_net),
        ])

        self.mon_foot_var = tk.StringVar(value="加载中…")
        ttk.Label(frame, textvariable=self.mon_foot_var,
                  style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
    def _toggle_monitor_timer(self, on: bool) -> None:
        if self._mon_after_id:
            self.root.after_cancel(self._mon_after_id)
            self._mon_after_id = None
        if on:
            self._mon_loop()

    def _mon_loop(self) -> None:
        if self.tab == "monitor":
            self._refresh_monitor()
            self._mon_after_id = self.root.after(MONITOR_INTERVAL_MS, self._mon_loop)
        else:
            self._mon_after_id = None

    def _refresh_monitor(self) -> None:
        def work():
            try:
                snap = monitor.snapshot(include_history=True)
            except Exception:
                snap = None
            if snap is not None:
                self._ui(lambda: self._apply_monitor_data(snap))

        threading.Thread(target=work, daemon=True).start()

    def _apply_monitor_data(self, snap: dict) -> None:
        if self.tab != "monitor":
            return
        self.mon_data = snap
        cpu, mem = snap["cpu"], snap["memory"]
        disk, net = snap["disk"], snap["network"]

        self.mon_vars["cpu"].set(f"{cpu['percent']}%")
        self.mon_vars["cpu_foot"].set(
            f"{cpu['cores']} 逻辑核心 / {cpu['physical_cores']} 物理核心"
            + (f" · {cpu['freq_mhz']} MHz" if cpu.get("freq_mhz") else ""))
        self.mon_vars["mem"].set(f"{mem['percent']}%")
        self.mon_vars["mem_foot"].set(
            f"{mem['used_text']} / {mem['total_text']}（可用 {mem['available_text']}）")
        self.mon_vars["disk"].set(f"读 {disk['read_speed_text']}")
        self.mon_vars["disk_foot"].set(
            f"写 {disk['write_speed_text']} · 累计读 {disk['read_total_text']} / 写 {disk['write_total_text']}")
        self.mon_vars["net"].set(f"↓ {net['recv_speed_text']}")
        self.mon_vars["net_foot"].set(
            f"↑ {net['sent_speed_text']} · 累计下行 {net['recv_total_text']} / 上行 {net['sent_total_text']}")

        # 磁盘分区条
        for widget in self.disk_box.winfo_children():
            widget.destroy()
        self.disk_rows.clear()
        for part in disk["partitions"]:
            row = tk.Frame(self.disk_box, bg=CARD)
            row.pack(fill="x", pady=2)
            name_var = tk.StringVar(value=f"{part['mountpoint']}  {part.get('fstype') or ''}")
            val_var = tk.StringVar(value=f"{part['used_text']} / {part['total_text']}（{part['percent']}%）")
            ttk.Label(row, textvariable=name_var, style="Card.TLabel", width=22).pack(side="left")
            bar = ttk.Progressbar(row, length=180, maximum=100, value=min(part["percent"], 100))
            bar.pack(side="left", padx=8)
            ttk.Label(row, textvariable=val_var, style="CardMuted.TLabel").pack(side="left")
            self.disk_rows.append((name_var, val_var, bar))
        if not disk["partitions"]:
            ttk.Label(self.disk_box, text="未检测到磁盘分区",
                      style="CardMuted.TLabel").pack(anchor="w")

        # 资源占用 Top：合并 CPU 榜与内存榜
        merged: dict[int, dict] = {}
        for p in snap.get("top_mem", []):
            merged[p["pid"]] = dict(p, cpu=None)
        for p in snap.get("top_cpu", []):
            merged.setdefault(p["pid"], dict(p, memory=p.get("memory"), memory_text=p.get("memory_text")))
            merged[p["pid"]]["cpu"] = p.get("cpu")
        rows = sorted(merged.values(),
                      key=lambda r: r.get("memory") or 0, reverse=True)
        self.top_tree.delete(*self.top_tree.get_children())
        for idx, r in enumerate(rows):
            self.top_tree.insert("", "end", values=(
                clip(r.get("name"), 40), r.get("pid"),
                "-" if r.get("cpu") is None else f"{r['cpu']}%",
                r.get("memory_text") or "-"),
                tags=(("striped",) if idx % 2 else ()))

        self.iface_tree.delete(*self.iface_tree.get_children())
        for idx, i in enumerate(net.get("interfaces", [])):
            self.iface_tree.insert("", "end", values=(
                clip(i["name"], 40), "已连接" if i["up"] else "未连接",
                i.get("ipv4") or "-",
                f"{i['speed_mbps']} Mbps" if i.get("speed_mbps") else "-",
                f"{i.get('recv_text', '-')} / {i.get('sent_text', '-')}"),
                tags=(("striped",) if idx % 2 else ()))

        self.mon_foot_var.set(
            f"主机名 {net.get('hostname', '-')} · CPU {cpu.get('name', '-')} · "
            f"开机时长 {snap.get('uptime_text', '-')} · 开机时间 {snap.get('boot_time', '-')} · "
            f"数据每 2 秒刷新")

        self._draw_chart()
        self._draw_cores()

    def _draw_chart(self) -> None:
        canvas = self.chart_canvas
        if not canvas:
            return
        canvas.delete("all")
        w = max(canvas.winfo_width(), 320)
        h = 170
        pad_l, pad_r, pad_t, pad_b = 40, 12, 10, 22
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b
        if plot_w <= 0:
            return

        for p in range(0, 101, 25):
            y = pad_t + plot_h - plot_h * p / 100
            canvas.create_line(pad_l, y, pad_l + plot_w, y,
                               fill="#dfe4ec" if p == 0 else "#eef1f6")
            canvas.create_text(14, y, text=f"{p}%", fill="#6b7280", font=("Microsoft YaHei", 8))

        hist = (self.mon_data or {}).get("history") or []
        if len(hist) < 2:
            canvas.create_text(pad_l + 10, pad_t + plot_h / 2, anchor="w",
                               text="正在采集历史数据…", fill="#6b7280", font=("Microsoft YaHei", 9))
            return

        n = len(hist)
        px = lambda i: pad_l + plot_w * i / (n - 1)
        py = lambda v: pad_t + plot_h - plot_h * max(0, min(100, v)) / 100

        for key, color in (("mem", "#ea580c"), ("cpu", "#2563eb")):
            pts = []
            for i, item in enumerate(hist):
                pts.extend((px(i), py(float(item.get(key) or 0))))
            canvas.create_line(*pts, fill=color, width=2, smooth=True)
        canvas.create_text(pad_l, h - 8, anchor="w", text=hist[0].get("time", ""),
                           fill="#6b7280", font=("Microsoft YaHei", 8))
        canvas.create_text(pad_l + plot_w, h - 8, anchor="e", text=hist[-1].get("time", ""),
                           fill="#6b7280", font=("Microsoft YaHei", 8))
        self.chart_range.set(f"最近 {n} 个采样点（每 2 秒一次，约 {max(1, round(n * 2 / 60))} 分钟）")

    def _draw_cores(self) -> None:
        canvas = self.core_canvas
        if not canvas:
            return
        canvas.delete("all")
        cores = (self.mon_data or {}).get("cpu", {}).get("per_core") or []
        if not cores:
            canvas.create_text(10, 38, anchor="w", text="正在采集…",
                               fill="#6b7280", font=("Microsoft YaHei", 9))
            return
        w = max(canvas.winfo_width(), 320)
        h = 76
        n = len(cores)
        gap = 6
        bw = max(6, (w - gap * (n + 1)) / n)
        for i, v in enumerate(cores):
            x0 = gap + i * (bw + gap)
            x1 = x0 + bw
            top = 8 + (h - 30) * (1 - min(v, 100) / 100)
            canvas.create_rectangle(x0, 8, x1, h - 22, fill="#eef1f6", outline="")
            canvas.create_rectangle(x0, top, x1, h - 22, fill="#2563eb", outline="")
            canvas.create_text((x0 + x1) / 2, h - 11,
                               text=(f"{i}:{v:g}" if bw >= 34 else f"{v:g}"),
                               fill="#6b7280", font=("Microsoft YaHei", 8))

    # ---------------- 清理页签 ----------------

    def _build_cleanup_tab(self, frame: ttk.Frame) -> None:

        # --- 微信清理相关状态 ---
        self.wx_tree = None
        self.wx_rows = []
        self.wx_sort = "size"       # size | time
        self.wx_order = "desc"      # desc | asc
        self.wx_kind_var = tk.StringVar(value="all")
        self.wx_total_var = tk.StringVar(value="双击行可勾选 / 取消")
        self.wx_summary_var = tk.StringVar(value="点击「扫描」查看微信缓存 / 照片 / 视频占用…")

        # --- 软件清理（已安装软件 + AI 工具缓存，合并展示）相关状态 ---
        self.soft_all_tree = None
        self.soft_file_tree = None
        self.soft_file_frame = None
        self.soft_items = []
        self.soft_rows = []
        self.soft_counts = {}
        self.soft_checked = {}
        self.soft_target = ""
        self.soft_hint = ""
        self.soft_label = ""
        self.soft_sort = "cache"       # cache | size | name
        self.soft_order = "desc"       # desc | asc
        self.soft_file_sort = "size"   # size | time
        self.soft_file_order = "desc"
        self.soft_loaded = False
        self.soft_all_summary_var = tk.StringVar(
            value="打开本页会自动扫描；也可点「扫描全部软件」重新扫描…")
        self.soft_all_total_var = tk.StringVar(value="双击行可勾选 / 取消")
        self.soft_file_total_var = tk.StringVar(value="双击行可勾选 / 取消")
        self.soft_filter_var = tk.StringVar(value="")
        self.soft_source_var = tk.StringVar(value="all")
        self.soft_scope_var = tk.StringVar(value="cache")

        # --- 文件查找相关状态 ---
        self.fs_tree = None
        self.fs_rows = []
        self.fs_hint = ""
        self.fs_sort = "size"          # size | time | name
        self.fs_order = "desc"
        self.fs_drive_values = [""]
        self.fs_key_var = tk.StringVar(value="")
        self.fs_root_var = tk.StringVar(value="")
        self.fs_drive_var = tk.StringVar(value="")
        self.fs_mode_var = tk.StringVar(value="文件 + 文件夹")
        self.fs_seconds_var = tk.StringVar(value="20")
        self.fs_ext_var = tk.StringVar(value="")
        self.fs_minmb_var = tk.StringVar(value="0")
        self.fs_depth_var = tk.StringVar(value="7")
        self.fs_dirsize_var = tk.BooleanVar(value=False)
        self.fs_summary_var = tk.StringVar(
            value="输入文件 / 文件夹名称关键字后回车，即可查找。")
        self.fs_total_var = tk.StringVar(value="双击行可勾选 / 取消")

        def build_mem(f):
            mem_card = ttk.LabelFrame(f, text="内存清理", padding=(14, 12))
            mem_card.pack(fill="x")
            info = tk.Frame(mem_card, bg=CARD)
            info.pack(side="left")
            self.c_mem_pct = tk.StringVar(value="-")
            self.c_mem_text = tk.StringVar(value="-")
            ttk.Label(info, text="当前内存占用", style="CardMuted.TLabel").pack(anchor="w")
            ttk.Label(info, textvariable=self.c_mem_pct, style="Stat.TLabel").pack(anchor="w", pady=(2, 0))
            ttk.Label(info, textvariable=self.c_mem_text, style="CardMuted.TLabel").pack(anchor="w")
            opts = tk.Frame(mem_card, bg=CARD)
            opts.pack(side="left", padx=26)
            ttk.Checkbutton(opts, text="同时清理系统文件缓存", style="Card.TCheckbutton",
                            variable=self.clean_opt_file_cache).pack(anchor="w")
            ttk.Checkbutton(opts, text="同时清理备用内存列表（需管理员权限）", style="Card.TCheckbutton",
                            variable=self.clean_opt_standby).pack(anchor="w", pady=(4, 0))
            ttk.Button(opts, text="一键清理内存", style="Primary.TButton",
                       command=self.clean_memory).pack(anchor="w", pady=(8, 0))
            self.c_mem_result = tk.StringVar(
                value="工作集裁剪会把各进程闲置的物理页换出到备用列表，不会关闭程序、不会丢失数据。")
            ttk.Label(mem_card, textvariable=self.c_mem_result, style="Card.TLabel",
                      foreground=PRIMARY, wraplength=520).pack(side="left", fill="x", expand=True)

        def build_disk(f):
            disk_card = ttk.LabelFrame(f, text="磁盘空间清理", padding=(14, 12))
            disk_card.pack(fill="x")
            bar = tk.Frame(disk_card, bg=CARD)
            bar.pack(fill="x")
            ttk.Button(bar, text="扫描占用", command=self.scan_clean_targets).pack(side="left")
            ttk.Button(bar, text="选择安全项", command=lambda: self.select_targets(True)).pack(side="left", padx=8)
            ttk.Button(bar, text="清空选择", command=lambda: self.select_targets(False)).pack(side="left")
            ttk.Button(bar, text="清空回收站", command=self.empty_bin).pack(side="left", padx=8)
            ttk.Button(bar, text="清理选中项", style="Danger.TButton",
                       command=self.clean_disk_selected).pack(side="right")
            self.target_total_var = tk.StringVar(value="双击行可勾选 / 取消")
            ttk.Label(bar, textvariable=self.target_total_var,
                      style="CardMuted.TLabel").pack(side="right", padx=14)
            t_cols = ("sel", "name", "size", "risk", "desc")
            self.target_tree = ttk.Treeview(disk_card, columns=t_cols, show="headings",
                                            height=8, style="Plain.Treeview")
            self.target_tree.tag_configure("striped", background="#fafbfe")
            for cid, title, width, anchor in (("sel", "选中", 48, "center"),
                                              ("name", "清理项", 190, "w"),
                                              ("size", "占用", 90, "e"),
                                              ("risk", "风险", 70, "center"),
                                              ("desc", "说明", 420, "w")):
                self.target_tree.heading(cid, text=title)
                self.target_tree.column(cid, width=width, anchor=anchor, stretch=(cid == "desc"))
            self.target_tree.pack(fill="x", pady=(8, 0))
            self.target_tree.bind("<Double-1>", self.toggle_target_row)

        def build_wechat(f):
            wx_card = ttk.LabelFrame(f, text="微信缓存 / 聊天文件清理", padding=(14, 12))
            wx_card.pack(fill="both", expand=True)
            wbar = tk.Frame(wx_card, bg=CARD)
            wbar.pack(fill="x")
            ttk.Label(wbar, text="类型：", style="CardMuted.TLabel").pack(side="left")
            self.wx_kind_combo = ttk.Combobox(wbar, state="readonly", width=12,
                                              textvariable=self.wx_kind_var)
            self.wx_kind_combo["values"] = ("all", "cache", "image", "video", "file")
            self.wx_kind_combo.current(0)
            self.wx_kind_combo.pack(side="left")
            ttk.Button(wbar, text="全选当前分类",
                       command=self.check_all_wechat).pack(side="left", padx=(10, 0))
            ttk.Button(wbar, text="扫描", command=self.scan_wechat).pack(side="left", padx=12)
            ttk.Button(wbar, text="删除选中（送回收站）", style="Danger.TButton",
                       command=self.delete_selected_wechat).pack(side="right")
            ttk.Label(wbar, textvariable=self.wx_total_var,
                      style="CardMuted.TLabel").pack(side="right", padx=14)

            ttk.Label(wx_card, textvariable=self.wx_summary_var,
                      style="CardMuted.TLabel", wraplength=1100).pack(anchor="w", pady=(6, 0))

            w_cols = ("sel", "name", "kind", "size", "mtime", "path")
            self.wx_tree = ttk.Treeview(wx_card, columns=w_cols, show="headings",
                                        height=10, style="Plain.Treeview")
            self.wx_tree.tag_configure("striped", background="#fafbfe")
            for cid, title, width, anchor in (("sel", "选中", 48, "center"),
                                              ("name", "文件名", 170, "w"),
                                              ("kind", "类型", 80, "center"),
                                              ("size", "大小", 90, "e"),
                                              ("mtime", "修改时间", 140, "center"),
                                              ("path", "路径", 520, "w")):
                self.wx_tree.heading(cid, text=title)
                self.wx_tree.column(cid, width=width, anchor=anchor, stretch=(cid == "path"))
            # 点击「大小」「修改时间」列头即可切换排序
            self.wx_tree.heading("size", command=lambda: self.sort_wechat("size"))
            self.wx_tree.heading("mtime", command=lambda: self.sort_wechat("time"))
            w_vsb = ttk.Scrollbar(wx_card, orient="vertical", command=self.wx_tree.yview)
            w_hsb = ttk.Scrollbar(wx_card, orient="horizontal", command=self.wx_tree.xview)
            self.wx_tree.configure(yscrollcommand=w_vsb.set, xscrollcommand=w_hsb.set)
            w_pane = tk.Frame(wx_card, bg=CARD)
            w_pane.pack(fill="both", expand=True, pady=(8, 0))
            self.wx_tree.grid(in_=w_pane, row=0, column=0, sticky="nsew")
            w_vsb.grid(in_=w_pane, row=0, column=1, sticky="ns")
            w_hsb.grid(in_=w_pane, row=1, column=0, sticky="ew")
            w_pane.grid_rowconfigure(0, weight=1)
            w_pane.grid_columnconfigure(0, weight=1)
            self.wx_tree.bind("<Double-1>", self.toggle_wechat_row)

        def build_agent(f):
            soft_card = ttk.LabelFrame(f, text="软件清理 · 已安装软件 / 缓存", padding=(14, 12))
            soft_card.pack(fill="both", expand=True)

            bar = tk.Frame(soft_card, bg=CARD)
            bar.pack(fill="x")
            ttk.Label(bar, text="过滤：", style="CardMuted.TLabel").pack(side="left")
            filter_entry = ttk.Entry(bar, width=16, textvariable=self.soft_filter_var)
            filter_entry.pack(side="left")
            filter_entry.bind("<Return>", lambda e: self.render_soft_all())
            ttk.Button(bar, text="应用过滤",
                       command=self.render_soft_all).pack(side="left", padx=(6, 0))
            ttk.Label(bar, text="来源：", style="CardMuted.TLabel").pack(side="left", padx=(14, 0))
            src_combo = ttk.Combobox(bar, state="readonly", width=12,
                                     values=("全部来源", "已安装软件", "AI 工具缓存"),
                                     textvariable=self.soft_source_var)
            src_combo.current(0)
            src_combo.pack(side="left")
            src_combo.bind("<<ComboboxSelected>>", lambda e: self.render_soft_all())
            ttk.Button(bar, text="扫描全部软件", style="Primary.TButton",
                       command=self.scan_soft_all).pack(side="left", padx=14)
            ttk.Button(bar, text="全选",
                       command=self.select_all_soft_rows).pack(side="left")
            ttk.Button(bar, text="卸载选中",
                       command=self.do_uninstall_soft).pack(side="left", padx=8)
            ttk.Button(bar, text="清理选中缓存", style="Danger.TButton",
                       command=self.do_clean_soft_selected).pack(side="right")
            ttk.Label(bar, textvariable=self.soft_all_total_var,
                      style="CardMuted.TLabel").pack(side="right", padx=14)

            ttk.Label(soft_card, textvariable=self.soft_all_summary_var,
                      style="CardMuted.TLabel", wraplength=1100).pack(anchor="w", pady=(6, 0))

            s_cols = ("sel", "name", "source", "pub", "size", "cache", "loc")
            self.soft_all_tree = ttk.Treeview(soft_card, columns=s_cols, show="headings",
                                              height=13, style="Plain.Treeview")
            self.soft_all_tree.tag_configure("striped", background="#fafbfe")
            self.soft_all_tree.tag_configure("onrow", background=ACTIVE_BG)
            for cid, title, width, anchor in (("sel", "选中", 48, "center"),
                                              ("name", "软件名称", 210, "w"),
                                              ("source", "来源", 92, "center"),
                                              ("pub", "厂商 / 版本", 190, "w"),
                                              ("size", "体积", 96, "e"),
                                              ("cache", "可清理", 96, "e"),
                                              ("loc", "安装位置", 320, "w")):
                self.soft_all_tree.heading(cid, text=title)
                self.soft_all_tree.column(cid, width=width, anchor=anchor,
                                          stretch=(cid == "loc"))
            # 点击列头切换排序
            self.soft_all_tree.heading("name", command=lambda: self.sort_soft_all("name"))
            self.soft_all_tree.heading("size", command=lambda: self.sort_soft_all("size"))
            self.soft_all_tree.heading("cache", command=lambda: self.sort_soft_all("cache"))
            s_vsb = ttk.Scrollbar(soft_card, orient="vertical",
                                  command=self.soft_all_tree.yview)
            s_hsb = ttk.Scrollbar(soft_card, orient="horizontal",
                                  command=self.soft_all_tree.xview)
            self.soft_all_tree.configure(yscrollcommand=s_vsb.set, xscrollcommand=s_hsb.set)
            s_pane = tk.Frame(soft_card, bg=CARD)
            s_pane.pack(fill="both", expand=True, pady=(8, 0))
            self.soft_all_tree.grid(in_=s_pane, row=0, column=0, sticky="nsew")
            s_vsb.grid(in_=s_pane, row=0, column=1, sticky="ns")
            s_hsb.grid(in_=s_pane, row=1, column=0, sticky="ew")
            s_pane.grid_rowconfigure(0, weight=1)
            s_pane.grid_columnconfigure(0, weight=1)
            self.soft_all_tree.bind("<<TreeviewSelect>>", self.on_soft_all_select)
            self.soft_all_tree.bind("<Double-1>", self.toggle_soft_all_row)

            # ---- 选中软件的可清理文件 ----
            self.soft_file_frame = ttk.LabelFrame(f, text="可清理文件", padding=(14, 12))
            self.soft_file_frame.pack(fill="both", expand=True, pady=(12, 0))
            fbar = tk.Frame(self.soft_file_frame, bg=CARD)
            fbar.pack(fill="x")
            ttk.Label(fbar, text="范围：", style="CardMuted.TLabel").pack(side="left")
            scope_combo = ttk.Combobox(fbar, state="readonly", width=12,
                                       values=("仅缓存 / 日志", "全部文件"),
                                       textvariable=self.soft_scope_var)
            scope_combo.current(0)
            scope_combo.pack(side="left")
            scope_combo.bind("<<ComboboxSelected>>", lambda e: self.load_soft_files())
            ttk.Button(fbar, text="全选",
                       command=self.check_all_soft_files).pack(side="left", padx=12)
            ttk.Button(fbar, text="删除选中（送回收站）", style="Danger.TButton",
                       command=self.delete_selected_soft_files).pack(side="right")
            ttk.Label(fbar, textvariable=self.soft_file_total_var,
                      style="CardMuted.TLabel").pack(side="right", padx=14)

            sf_cols = ("sel", "name", "size", "mtime", "path")
            self.soft_file_tree = ttk.Treeview(self.soft_file_frame, columns=sf_cols,
                                               show="headings", height=10,
                                               style="Plain.Treeview")
            self.soft_file_tree.tag_configure("striped", background="#fafbfe")
            for cid, title, width, anchor in (("sel", "选中", 48, "center"),
                                              ("name", "文件名", 200, "w"),
                                              ("size", "大小", 96, "e"),
                                              ("mtime", "修改时间", 140, "center"),
                                              ("path", "路径", 480, "w")):
                self.soft_file_tree.heading(cid, text=title)
                self.soft_file_tree.column(cid, width=width, anchor=anchor,
                                           stretch=(cid == "path"))
            self.soft_file_tree.heading("size", command=lambda: self.sort_soft_file("size"))
            self.soft_file_tree.heading("mtime", command=lambda: self.sort_soft_file("time"))
            sf_vsb = ttk.Scrollbar(self.soft_file_frame, orient="vertical",
                                   command=self.soft_file_tree.yview)
            sf_hsb = ttk.Scrollbar(self.soft_file_frame, orient="horizontal",
                                   command=self.soft_file_tree.xview)
            self.soft_file_tree.configure(yscrollcommand=sf_vsb.set,
                                          xscrollcommand=sf_hsb.set)
            sf_pane = tk.Frame(self.soft_file_frame, bg=CARD)
            sf_pane.pack(fill="both", expand=True, pady=(8, 0))
            self.soft_file_tree.grid(in_=sf_pane, row=0, column=0, sticky="nsew")
            sf_vsb.grid(in_=sf_pane, row=0, column=1, sticky="ns")
            sf_hsb.grid(in_=sf_pane, row=1, column=0, sticky="ew")
            sf_pane.grid_rowconfigure(0, weight=1)
            sf_pane.grid_columnconfigure(0, weight=1)
            self.soft_file_tree.bind("<Double-1>", self.toggle_soft_file_row)

        def build_files(f):
            file_card = ttk.LabelFrame(f, text="大文件", padding=(14, 12))
            file_card.pack(fill="both", expand=True)
            fbar = tk.Frame(file_card, bg=CARD)
            fbar.pack(fill="x")
            ttk.Label(fbar, text="分区：", style="CardMuted.TLabel").pack(side="left")
            self.drive_combo = ttk.Combobox(fbar, state="readonly", width=18,
                                            textvariable=self.drive_var)
            self.drive_combo.pack(side="left")
            ttk.Label(fbar, text="大小 ≥", style="CardMuted.TLabel").pack(side="left", padx=(16, 0))
            ttk.Spinbox(fbar, from_=10, to=10240, width=6,
                        textvariable=self.minmb_var).pack(side="left")
            ttk.Label(fbar, text="MB", style="CardMuted.TLabel").pack(side="left", padx=(4, 0))
            ttk.Button(fbar, text="扫描大文件", command=self.scan_big_files).pack(side="left", padx=12)
            ttk.Button(fbar, text="删除选中（送回收站）", style="Danger.TButton",
                       command=self.delete_selected_files).pack(side="right")
            self.file_total_var = tk.StringVar(value="双击行可勾选 / 取消")
            ttk.Label(fbar, textvariable=self.file_total_var,
                      style="CardMuted.TLabel").pack(side="right", padx=14)
            f_cols = ("sel", "name", "size", "cat", "risk", "path")
            self.file_tree = ttk.Treeview(file_card, columns=f_cols, show="headings",
                                          height=10, style="Plain.Treeview")
            self.file_tree.tag_configure("striped", background="#fafbfe")
            for cid, title, width, anchor in (("sel", "选中", 48, "center"),
                                              ("name", "文件名", 180, "w"),
                                              ("size", "大小", 90, "e"),
                                              ("cat", "类别", 110, "w"),
                                              ("risk", "风险", 70, "center"),
                                              ("path", "路径", 540, "w")):
                self.file_tree.heading(cid, text=title)
                self.file_tree.column(cid, width=width, anchor=anchor, stretch=(cid == "path"))
            f_vsb = ttk.Scrollbar(file_card, orient="vertical", command=self.file_tree.yview)
            f_hsb = ttk.Scrollbar(file_card, orient="horizontal", command=self.file_tree.xview)
            self.file_tree.configure(yscrollcommand=f_vsb.set, xscrollcommand=f_hsb.set)
            tree_pane = tk.Frame(file_card, bg=CARD)
            tree_pane.pack(fill="both", expand=True, pady=(8, 0))
            self.file_tree.grid(in_=tree_pane, row=0, column=0, sticky="nsew")
            f_vsb.grid(in_=tree_pane, row=0, column=1, sticky="ns")
            f_hsb.grid(in_=tree_pane, row=1, column=0, sticky="ew")
            tree_pane.grid_rowconfigure(0, weight=1)
            tree_pane.grid_columnconfigure(0, weight=1)
            self.file_tree.bind("<Double-1>", self.toggle_file_row)

        self._make_treenav(frame, "磁盘清理", [
            ("mem", "内存清理", build_mem),
            ("disk", "磁盘空间", build_disk),
            ("files", "大文件", build_files),
            ("wechat", "微信清理", build_wechat),
            ("agent", "软件清理", build_agent),
        ])

        tip = ttk.Label(frame, style="Muted.TLabel", wraplength=1200, text=(
            "删除的文件默认送入回收站，可随时还原。位于 Windows / Program Files 等系统保护目录的文件"
            "会被自动跳过，无法通过该工具删除；标记为「高风险」的项目请确认后果后再操作。"
            "「软件清理」把已安装软件与 AI 编程工具缓存合并展示，卸载命令取自系统登记的卸载信息。"))
        tip.pack(anchor="w", pady=(8, 0))

        self.root.after(300, self.load_drives)
    # ---- 通用：后台执行清理动作 ----

    def _ui(self, fn) -> None:
        """把回调投递回 Tk 主线程；窗口已销毁时静默丢弃。

        扫描/清理都在后台线程执行，若在窗口关闭后才返回，
        直接调用 root.after 会抛出 "main thread is not in main loop"。
        """
        try:
            if self.root and self.root.winfo_exists():
                self.root.after(0, fn)
        except Exception:
            pass

    def _clean_async(self, fn, title: str, on_done=None) -> None:
        if self.clean_busy:
            return
        self.clean_busy = True
        self.progress.start(12)
        self.status_var.set(f"正在{title}…")

        def work():
            try:
                result = fn()
            except Exception as exc:
                result = exc
            self._ui(lambda: self._clean_finish(result, title, on_done))

        threading.Thread(target=work, daemon=True).start()

    def _clean_finish(self, result, title: str, on_done=None) -> None:
        self.clean_busy = False
        self.progress.stop()
        if isinstance(result, Exception):
            self.status_var.set(f"{title}失败：{result}")
            messagebox.showerror(title, f"{title}失败：\n{result}")
            return
        self.status_var.set(f"{title}完成")
        if on_done:
            on_done(result)

    def load_drives(self) -> None:
        """读取可用分区；list_drives 只做 disk_usage，耗时可忽略，直接同步调用。"""
        try:
            drives = cleanup.list_drives()
        except Exception:
            return
        self.drive_values = [""] + [d["mountpoint"] for d in drives]
        self.drive_combo["values"] = (
            ["全部分区"] + [f"{d['mountpoint']}  可用 {d['free_text']}" for d in drives])
        self.drive_combo.current(0)
        combo = getattr(self, "soft_drive_combo", None)
        if combo is not None:
            combo["values"] = self.drive_combo["values"]
            combo.current(0)

    # ---- 内存 ----

    def update_mem_card(self) -> None:
        snap = self.mon_data
        if not snap:
            try:
                snap = monitor.snapshot(include_history=False)
            except Exception:
                return
        mem = snap.get("memory", {})
        self.c_mem_pct.set(f"{mem.get('percent')}%")
        self.c_mem_text.set(
            f"{mem.get('used_text')} / {mem.get('total_text')}（可用 {mem.get('available_text')}）")

    def clean_memory(self) -> None:
        def work():
            return cleanup.clean_memory(
                trim_working_set=True,
                clear_file_cache=self.clean_opt_file_cache.get(),
                purge_standby=self.clean_opt_standby.get())

        def done(r):
            if isinstance(r, Exception):
                return
            self.c_mem_result.set(
                f"清理完成：{r['before_percent']}% → {r['after_percent']}%"
                f"（{r['before_text']} → {r['after_text']}），释放 {r['freed_text']}")
            self.update_mem_card()

        self._clean_async(work, "内存清理", done)

    # ---- 磁盘清理项 ----

    def scan_clean_targets(self) -> None:
        if self.target_tree is not None:
            self.target_tree.delete(*self.target_tree.get_children())

        def work():
            return cleanup.scan_targets()

        def done(r):
            if isinstance(r, Exception):
                return
            self.clean_targets = r["rows"]
            self._render_clean_targets()

        self._clean_async(work, "扫描可清理项", done)

    def _render_clean_targets(self) -> None:
        tree = self.target_tree
        if tree is None:
            return
        tree.delete(*tree.get_children())
        risk_text = {"safe": "安全", "medium": "一般", "high": "高风险"}
        for idx, t in enumerate(self.clean_targets):
            tree.insert("", "end", iid=t["id"], values=(
                "[√]" if t.get("_checked") else "[ ]",
                t["name"], t["size_text"] if t["available"] else "-",
                risk_text.get(t["risk"], t["risk"]),
                t["desc"] if t["available"] else "该路径在本机不存在"),
                tags=(("striped",) if idx % 2 else ()))
            if not t["available"] or not t["size"]:
                tree.set(t["id"], "sel", "[-]")
        self._update_target_total()

    def toggle_target_row(self, event=None) -> None:
        tree = self.target_tree
        iid = tree.identify_row(event.y) if event else None
        if not iid:
            sel = tree.selection()
            iid = sel[0] if sel else None
        if not iid:
            return
        row = next((t for t in self.clean_targets if t["id"] == iid), None)
        if row is None or not row["available"] or not row["size"]:
            return
        row["_checked"] = not row.get("_checked")
        tree.set(iid, "sel", "[√]" if row["_checked"] else "[ ]")
        self._update_target_total()

    def select_targets(self, only_safe: bool) -> None:
        for t in self.clean_targets:
            t["_checked"] = bool(only_safe and t["risk"] == "safe" and t["available"] and t["size"])
        self._render_clean_targets()

    def _update_target_total(self) -> None:
        picked = [t for t in self.clean_targets if t.get("_checked")]
        total = sum(t["size"] for t in picked)
        self.target_total_var.set(f"已选中 {len(picked)} 项 · 预计释放 {cleanup.human_bytes(total)}")

    def clean_disk_selected(self) -> None:
        picked = [t for t in self.clean_targets
                  if t.get("_checked") and t["available"] and t["size"]]
        if not picked:
            messagebox.showinfo("清理磁盘", "请先双击勾选要清理的项目。")
            return
        has_high = any(t["risk"] == "high" for t in picked)
        text = "\n".join(f"· {t['name']}（{t['size_text']}）" for t in picked)
        ok = messagebox.askyesno(
            "确认清理",
            f"即将清理 {len(picked)} 项：\n{text}\n\n"
            f"预计释放 {cleanup.human_bytes(sum(t['size'] for t in picked))}。"
            + ("\n\n⚠ 其中包含高风险项目，清理后可能无法恢复。" if has_high else ""))
        if not ok:
            return

        ids = [t["id"] for t in picked]

        def work():
            return cleanup.clean_disk(ids, dry_run=False)

        def done(r):
            if isinstance(r, Exception):
                return
            okres, payload = r
            for t in self.clean_targets:
                t["_checked"] = False
            messagebox.showinfo("清理磁盘", payload.get("message", "清理完成"))
            self.scan_clean_targets()

        self._clean_async(work, "磁盘清理", done)

    def empty_bin(self) -> None:
        if not messagebox.askyesno("清空回收站", "确定要清空回收站吗？其中的文件将无法恢复。"):
            return

        def work():
            return cleanup.empty_recycle_bin()

        def done(r):
            if isinstance(r, Exception):
                return
            okres, msg = r
            (messagebox.showinfo if okres else messagebox.showerror)("清空回收站", msg)

        self._clean_async(work, "清空回收站", done)

    # ---- 大文件 ----

    def scan_big_files(self) -> None:
        if self.file_tree is not None:
            self.file_tree.delete(*self.file_tree.get_children())
        drive_idx = self.drive_combo.current()
        drive = (getattr(self, "drive_values", [""]) or [""])[drive_idx] if drive_idx >= 0 else ""
        min_mb = self.minmb_var.get() or 200

        def work():
            return cleanup.scan_large_files(drive or None, min_mb=min_mb, limit=300)

        def done(r):
            if isinstance(r, Exception):
                return
            self.clean_files = r["rows"]
            self._render_big_files()
            tip = "（已达时间上限，结果可能不完整）" if r.get("timed_out") else ""
            self.status_var.set(
                f"扫描完成：找到 {r['total_found']} 个大文件，耗时 {r['elapsed']}s{tip}")

        self._clean_async(work, "大文件扫描", done)

    def _render_big_files(self) -> None:
        tree = self.file_tree
        if tree is None:
            return
        tree.delete(*tree.get_children())
        risk_text = {"low": "低", "medium": "中", "high": "高风险"}
        for i, f in enumerate(self.clean_files):
            iid = f"f{i}"
            tree.insert("", "end", iid=iid, values=(
                "[√]" if f.get("_checked") else "[ ]",
                clip(f["name"], 40), f["size_text"], f["category"],
                risk_text.get(f["risk"], f["risk"]), f["path"]),
                tags=(("striped",) if i % 2 else ()))
            if f["risk"] == "high" and f["category"] == "系统文件":
                tree.set(iid, "sel", "[-]")
        self._update_file_total()

    def toggle_file_row(self, event=None) -> None:
        tree = self.file_tree
        iid = tree.identify_row(event.y) if event else None
        if not iid:
            sel = tree.selection()
            iid = sel[0] if sel else None
        if not iid or not iid.startswith("f"):
            return
        idx = int(iid[1:])
        if idx >= len(self.clean_files):
            return
        f = self.clean_files[idx]
        if f["risk"] == "high" and f["category"] == "系统文件":
            return
        f["_checked"] = not f.get("_checked")
        tree.set(iid, "sel", "[√]" if f["_checked"] else "[ ]")
        self._update_file_total()

    def _update_file_total(self) -> None:
        picked = [f for f in self.clean_files if f.get("_checked")]
        total = sum(f["size"] for f in picked)
        self.file_total_var.set(
            f"已选中 {len(picked)} 个文件 · 合计 {cleanup.human_bytes(total)}")

    def delete_selected_files(self) -> None:
        picked = [f for f in self.clean_files if f.get("_checked")]
        if not picked:
            messagebox.showinfo("删除大文件", "请先双击勾选要删除的文件。")
            return
        text = "\n".join(f"· {f['name']}（{f['size_text']}）" for f in picked[:10])
        if len(picked) > 10:
            text += f"\n…以及另外 {len(picked) - 10} 个文件"
        if not messagebox.askyesno(
                "确认删除",
                f"即将把以下 {len(picked)} 个文件（合计 "
                f"{cleanup.human_bytes(sum(f['size'] for f in picked))}）送入回收站：\n{text}"
                "\n\n删除后可从回收站还原。"):
            return

        paths = [f["path"] for f in picked]

        def work():
            return cleanup.delete_files(paths, use_recycle=True)

        def done(r):
            if isinstance(r, Exception):
                return
            _ok, payload = r
            messagebox.showinfo("删除大文件",
                                payload.get("message", "已提交删除"))
            self.scan_big_files()

        self._clean_async(work, "删除大文件", done)

    # ---- 微信缓存 / 聊天文件 ----

    def scan_wechat(self) -> None:
        tree = self.wx_tree
        if tree is not None:
            tree.delete(*tree.get_children())
        kind = self.wx_kind_var.get() or "all"
        sort_by = getattr(self, "wx_sort", "size")
        order = getattr(self, "wx_order", "desc")

        def work():
            return (cleanup.wechat_summary(),
                    cleanup.scan_wechat_media(kind=kind, sort_by=sort_by,
                                              order=order, limit=500))

        def done(r):
            if isinstance(r, Exception):
                return
            summary, media = r
            self.wx_rows = media["rows"]
            if not summary["installed"]:
                self.wx_summary_var.set(
                    "未检测到微信数据目录（本机可能未安装微信，或聊天文件保存在其他位置）。")
            else:
                parts = [f"{g['name']} {g['size_text']}（{g['count']} 个文件）"
                         for g in summary["groups"]]
                self.wx_summary_var.set(
                    f"微信可清理合计 {summary['total_text']} · " + " · ".join(parts))
            self._render_wechat()
            self.status_var.set(
                f"微信扫描完成：{media['total_found']} 个文件，当前按"
                f"{'大小' if sort_by == 'size' else '时间'}"
                f"{'降序' if order == 'desc' else '升序'}")

        self._clean_async(work, "微信清理扫描", done)

    def sort_wechat(self, by: str) -> None:
        """点击列头切换排序；同一列再次点击反转升降序。"""
        if getattr(self, "wx_sort", "size") == by:
            self.wx_order = "asc" if getattr(self, "wx_order", "desc") == "desc" else "desc"
        else:
            self.wx_sort = by
            self.wx_order = "desc"
        self.scan_wechat()

    def _render_wechat(self) -> None:
        tree = self.wx_tree
        if tree is None:
            return
        tree.delete(*tree.get_children())
        for i, f in enumerate(self.wx_rows):
            iid = f"w{i}"
            tree.insert("", "end", iid=iid, values=(
                "[√]" if f.get("_checked") else "[ ]",
                clip(f["name"], 34), f["kind_text"], f["size_text"],
                f["mtime"], f["path"]), tags=(("striped",) if i % 2 else ()))
        self._update_wx_total()

    def toggle_wechat_row(self, event=None) -> None:
        tree = self.wx_tree
        if tree is None:
            return
        iid = tree.identify_row(event.y) if event else None
        if not iid:
            sel = tree.selection()
            iid = sel[0] if sel else None
        if not iid or not iid.startswith("w"):
            return
        idx = int(iid[1:])
        if idx >= len(self.wx_rows):
            return
        f = self.wx_rows[idx]
        f["_checked"] = not f.get("_checked")
        tree.set(iid, "sel", "[√]" if f["_checked"] else "[ ]")
        self._update_wx_total()

    def _update_wx_total(self) -> None:
        picked = [f for f in self.wx_rows if f.get("_checked")]
        total = sum(f["size"] for f in picked)
        self.wx_total_var.set(
            f"已选中 {len(picked)} 个文件 · 合计 {cleanup.human_bytes(total)}")

    def delete_selected_wechat(self) -> None:
        picked = [f for f in self.wx_rows if f.get("_checked")]
        if not picked:
            messagebox.showinfo("删除微信文件", "请先双击勾选要删除的文件。")
            return
        text = "\n".join(f"· {f['name']}（{f['size_text']}）" for f in picked[:10])
        if len(picked) > 10:
            text += f"\n…以及另外 {len(picked) - 10} 个文件"
        if not messagebox.askyesno(
                "确认删除",
                f"即将把以下 {len(picked)} 个微信文件（合计 "
                f"{cleanup.human_bytes(sum(f['size'] for f in picked))}）送入回收站：\n{text}"
                "\n\n删除后聊天记录中的图片 / 视频将无法查看，可从回收站还原。"):
            return

        paths = [f["path"] for f in picked]

        def work():
            return cleanup.delete_files(paths, use_recycle=True)

        def done(r):
            if isinstance(r, Exception):
                return
            _ok, payload = r
            messagebox.showinfo("删除微信文件", payload.get("message", "已提交删除"))
            self.scan_wechat()

        self._clean_async(work, "删除微信文件", done)

    # ---- Vibe Coding 工具缓存 ----

    def check_all_wechat(self) -> None:
        """把当前分类列表里的文件全部勾选 / 取消。"""
        rows = getattr(self, "wx_rows", []) or []
        if not rows:
            messagebox.showinfo("微信清理", "当前分类没有可勾选的文件。")
            return
        turn_on = any(not f.get("_checked") for f in rows)
        for f in rows:
            f["_checked"] = turn_on
        self._render_wechat()

    # ---- 软件清理：默认扫描本机全部软件（已安装软件 + AI 工具缓存） ----

    def _auto_scan_soft(self) -> None:
        """首次切到磁盘清理页时自动扫描全部软件（避免用户还要手动点按钮）。"""
        if not getattr(self, "soft_loaded", False):
            self.scan_soft_all()

    def scan_soft_all(self) -> None:
        def work():
            return software.software_cleanup_summary(max_seconds=25.0)

        def done(r):
            self.soft_items = r.get("items") or []
            self.soft_counts = r.get("counts") or {}
            self.soft_checked = {}
            self.soft_loaded = True
            self.soft_hint = ""
            self.soft_all_summary_var.set(
                f"{r.get('count', 0)} 项软件"
                f"（已安装 {self.soft_counts.get('installed', 0)} · "
                f"AI 工具缓存 {self.soft_counts.get('agents', 0)}"
                + (f" · 扫描发现 {self.soft_counts.get('discovered', 0)}"
                   if self.soft_counts.get('discovered') else "") + "）"
                f" · 可清理合计 {r.get('total_cache_text', '0 B')}"
                f" · 用时 {r.get('elapsed', 0)}s"
                + (" · 已触发时间上限，未测算项显示「—」" if r.get("timed_out") else ""))
            self.render_soft_all()
            if self.soft_target:
                self.load_soft_files()
            self.status_var.set(f"软件扫描完成：{r.get('count', 0)} 项")

        self._clean_async(work, "扫描全部软件", done)

    def soft_filtered_items(self) -> list:
        kw = (self.soft_filter_var.get() or "").strip().lower()
        source = SOFT_SOURCE_MAP.get(self.soft_source_var.get() or "全部来源", "all")
        items = list(self.soft_items or [])
        if source != "all":
            items = [i for i in items if i.get("source_key") == source]
        if kw:
            items = [i for i in items if (
                kw in str(i.get("name", "")).lower() or
                kw in str(i.get("publisher", "")).lower() or
                kw in str(i.get("location", "")).lower())]
        rev = self.soft_order == "desc"
        if self.soft_sort == "name":
            items.sort(key=lambda i: str(i.get("name", "")), reverse=rev)
        elif self.soft_sort == "size":
            items.sort(key=lambda i: int(i.get("size", 0) or 0), reverse=rev)
        else:
            items.sort(key=lambda i: int(i.get("cache_size", 0) or 0), reverse=rev)
        return items

    def render_soft_all(self) -> None:
        tree = self.soft_all_tree
        if tree is None:
            return
        tree.delete(*tree.get_children())
        items = self.soft_filtered_items()
        if not items:
            tree.insert("", "end", values=("[ ]", "（没有匹配的软件，点击「扫描全部软件」重试）",
                                           "", "", "", "", ""))
            self.update_soft_all_total()
            return
        for i, item in enumerate(items):
            tags = []
            if i % 2:
                tags.append("striped")
            if item.get("key") == self.soft_target:
                tags.append("onrow")
            version = item.get("version") or ""
            pub = str(item.get("publisher") or "—")
            tree.insert("", "end", iid=f"sg{i}", tags=tuple(tags), values=(
                "[√]" if self.soft_checked.get(item.get("key")) else "[ ]",
                clip(item.get("name", ""), 26),
                item.get("source", ""),
                clip(pub, 22) + ((" · v" + version) if version and version != "—" else ""),
                item.get("size_text", "—") if item.get("size", 0) > 0 else "—",
                item.get("cache_text", "—"),
                item.get("location") or "（未提供安装位置）"))
        self.update_soft_all_total()

    def on_soft_all_select(self, event=None) -> None:
        tree = self.soft_all_tree
        if tree is None:
            return
        sel = tree.selection()
        if not sel:
            return
        iid = sel[0]
        if not iid.startswith("sg"):
            return
        items = self.soft_filtered_items()
        idx = int(iid[2:])
        if idx >= len(items):
            return
        item = items[idx]
        key = item.get("key", "")
        if key == self.soft_target:
            return
        self.soft_target = key
        self.soft_label = item.get("name", "")
        self.soft_rows = []
        self.render_soft_all()
        self.load_soft_files()

    def toggle_soft_all_row(self, event=None) -> None:
        tree = self.soft_all_tree
        if tree is None:
            return
        iid = tree.identify_row(event.y) if event else None
        if not iid:
            sel = tree.selection()
            iid = sel[0] if sel else None
        if not iid or not iid.startswith("sg"):
            return
        items = self.soft_filtered_items()
        idx = int(iid[2:])
        if idx >= len(items):
            return
        key = items[idx].get("key")
        self.soft_checked[key] = not self.soft_checked.get(key)
        tree.set(iid, "sel", "[√]" if self.soft_checked.get(key) else "[ ]")
        self.update_soft_all_total()

    def select_all_soft_rows(self) -> None:
        items = self.soft_filtered_items()
        if not items:
            messagebox.showinfo("软件清理", "当前列表没有可选的项。")
            return
        turn_on = any(not self.soft_checked.get(i.get("key")) for i in items)
        for i in items:
            self.soft_checked[i.get("key")] = turn_on
        self.render_soft_all()

    def update_soft_all_total(self) -> None:
        picked = [i for i in (self.soft_items or []) if self.soft_checked.get(i.get("key"))]
        cache = sum(int(i.get("cache_size", 0) or 0) for i in picked)
        can_un = sum(1 for i in picked if i.get("can_uninstall"))
        self.soft_all_total_var.set(
            f"已选 {len(picked)} 项 · 可清理 {cleanup.human_bytes(cache)} · 可卸载 {can_un} 项")

    def sort_soft_all(self, by: str) -> None:
        if getattr(self, "soft_sort", "cache") == by:
            self.soft_order = "asc" if getattr(self, "soft_order", "desc") == "desc" else "desc"
        else:
            self.soft_sort = by
            self.soft_order = "desc"
        self.render_soft_all()

    # ---- 软件清理：选中软件的可清理文件 ----

    def load_soft_files(self) -> None:
        if getattr(self, "soft_file_frame", None) is not None:
            title = "可清理文件" + (f" · {self.soft_label}" if self.soft_target else "")
            self.soft_file_frame.configure(text=title)
        if not self.soft_target:
            self.soft_rows = []
            self.render_soft_files()
            return
        key = self.soft_target
        scope = SOFT_SCOPE_MAP.get(self.soft_scope_var.get() or "仅缓存 / 日志", "cache")
        sort_by = getattr(self, "soft_file_sort", "size")
        order = getattr(self, "soft_file_order", "desc")

        def work():
            return software.software_cache_files(key=key, scope=scope,
                                                 sort_by=sort_by, order=order,
                                                 limit=400)

        def done(r):
            self.soft_rows = r.get("rows") or []
            self.soft_hint = r.get("hint") or ""
            self.soft_file_dir_text = r.get("dir_size_text", "0 B")
            self.soft_file_count = r.get("dir_count", 0)
            self.soft_file_label = r.get("label", "")
            self.render_soft_files()
            if self.soft_hint:
                self.status_var.set(self.soft_hint)
            else:
                self.status_var.set(
                    f"{r.get('label', '')}：{r.get('total_found', 0)} 个文件，"
                    f"合计 {r.get('dir_size_text', '0 B')}")

        self._clean_async(work, "读取可清理文件", done)

    def render_soft_files(self) -> None:
        tree = self.soft_file_tree
        if tree is None:
            return
        tree.delete(*tree.get_children())
        rows = self.soft_rows or []
        if not rows and self.soft_hint:
            tree.insert("", "end", values=("[ ]", self.soft_hint, "", "", ""))
        for i, f in enumerate(rows):
            tree.insert("", "end", iid=f"sgf{i}", values=(
                "[√]" if f.get("_checked") else "[ ]",
                clip(f["name"], 30), f["size_text"], f["mtime"], f["path"]),
                tags=(("striped",) if i % 2 else ()))
        self.update_soft_file_total()

    def sort_soft_file(self, by: str) -> None:
        if getattr(self, "soft_file_sort", "size") == by:
            self.soft_file_order = ("asc" if getattr(self, "soft_file_order",
                                                     "desc") == "desc" else "desc")
        else:
            self.soft_file_sort = by
            self.soft_file_order = "desc"
        if self.soft_target:
            self.load_soft_files()

    def toggle_soft_file_row(self, event=None) -> None:
        tree = self.soft_file_tree
        if tree is None:
            return
        iid = tree.identify_row(event.y) if event else None
        if not iid:
            sel = tree.selection()
            iid = sel[0] if sel else None
        if not iid or not iid.startswith("sgf"):
            return
        idx = int(iid[3:])
        if idx >= len(self.soft_rows):
            return
        f = self.soft_rows[idx]
        f["_checked"] = not f.get("_checked")
        tree.set(iid, "sel", "[√]" if f["_checked"] else "[ ]")
        self.update_soft_file_total()

    def check_all_soft_files(self) -> None:
        if not self.soft_rows:
            messagebox.showinfo("软件清理", "请先选择一项软件。")
            return
        turn_on = any(not f.get("_checked") for f in self.soft_rows)
        for f in self.soft_rows:
            f["_checked"] = turn_on
        self.render_soft_files()

    def update_soft_file_total(self) -> None:
        picked = [f for f in (self.soft_rows or []) if f.get("_checked")]
        total = sum(f["size"] for f in picked)
        self.soft_file_total_var.set(
            f"已选 {len(picked)} 个文件 · 合计 {cleanup.human_bytes(total)}")

    def delete_selected_soft_files(self) -> None:
        picked = [f for f in (self.soft_rows or []) if f.get("_checked")]
        if not picked:
            messagebox.showinfo("软件清理", "请先双击勾选要删除的文件。")
            return
        total = sum(f["size"] for f in picked)
        if not messagebox.askyesno(
                "确认删除",
                f"即将把 {len(picked)} 个缓存文件"
                f"（合计 {cleanup.human_bytes(total)}）送入回收站：\n\n"
                + "\n".join("· " + f["name"] for f in picked[:10])
                + (f"\n…以及另外 {len(picked) - 10} 个" if len(picked) > 10 else "")
                + "\n\n这些都是缓存 / 日志类可再生数据，删除后软件会自动重建，"
                  "可从回收站还原。"):
            return

        def work():
            return cleanup.delete_files([f["path"] for f in picked], use_recycle=True)

        def done(r):
            if isinstance(r, Exception):
                messagebox.showerror("删除失败", str(r))
                return
            _ok, info = r
            messagebox.showinfo(
                "删除完成",
                f"已删除 {info.get('deleted', 0)} 个文件，释放 {info.get('freed_text', '0 B')}；"
                f"跳过 {info.get('skipped', 0)} 个受保护文件，失败 {info.get('failed', 0)} 个。")
            self.load_soft_files()

        self._clean_async(work, "删除缓存文件", done)

    def do_clean_soft_selected(self) -> None:
        picked = [i for i in (self.soft_items or []) if self.soft_checked.get(i.get("key"))]
        if not picked:
            messagebox.showinfo("软件清理", "请先勾选要清理的软件。")
            return
        has_cache = [i for i in picked if int(i.get("cache_size", 0) or 0) > 0 and i.get("paths")]
        if not has_cache:
            messagebox.showinfo(
                "软件清理",
                "选中的软件没有可识别的缓存目录：\n"
                "· 已安装软件通常要把缓存写在自身安装目录下才会被识别\n"
                "· AI 编程工具缓存会在扫描后自动列出\n\n"
                "如需腾出空间，请勾选缓存 > 0 B 的项，或使用「卸载选中」。")
            return
        total = sum(int(i.get("cache_size", 0) or 0) for i in has_cache)
        if not messagebox.askyesno(
                "确认清理",
                f"即将清理 {len(has_cache)} 项软件的缓存"
                f"（合计约 {cleanup.human_bytes(total)}）：\n\n"
                + "\n".join("· " + i["name"] + "（" + i.get("cache_text", "") + "）"
                            for i in has_cache[:12])
                + (f"\n…以及另外 {len(has_cache) - 12} 项" if len(has_cache) > 12 else "")
                + "\n\n只清理缓存 / 日志 / 会话历史等可再生数据，"
                  "账号凭证、用户配置与程序本体不会被删除，文件送入回收站可还原。"):
            return

        paths: list[str] = []
        for i in has_cache:
            paths.extend(i.get("paths") or [])

        def work():
            return cleanup.delete_files(paths, use_recycle=True)

        def done(r):
            if isinstance(r, Exception):
                messagebox.showerror("清理失败", str(r))
                return
            _ok, info = r
            messagebox.showinfo(
                "清理完成",
                f"已删除 {info.get('deleted', 0)} 个文件，释放 {info.get('freed_text', '0 B')}；"
                f"跳过 {info.get('skipped', 0)} 个受保护文件，失败 {info.get('failed', 0)} 个。")
            self.scan_soft_all()

        self._clean_async(work, "清理软件缓存", done)

    def do_uninstall_soft(self) -> None:
        picked = [i for i in (self.soft_items or []) if self.soft_checked.get(i.get("key"))]
        can = [i for i in picked if i.get("can_uninstall")]
        if not can:
            messagebox.showinfo(
                "软件清理",
                "请勾选支持卸载的已安装软件（来源列显示「已安装」的项）。")
            return
        skipped = len(picked) - len(can)
        if not messagebox.askyesno(
                "确认卸载",
                f"即将调用软件自带的卸载程序卸载 {len(can)} 个软件：\n\n"
                + "\n".join("· " + i["name"] for i in can[:12])
                + (f"\n…以及另外 {len(can) - 12} 个" if len(can) > 12 else "")
                + (f"\n\n另有 {skipped} 项未提供卸载命令，已跳过。" if skipped else "")
                + "\n\n卸载命令取自系统登记的卸载信息，"
                  "实际进度请在随后弹出的卸载窗口中完成。"):
            return

        def work():
            msgs = []
            for i in can:
                try:
                    ok, msg = software.uninstall_software(i.get("ident", ""), quiet=False)
                except Exception as exc:
                    ok, msg = False, str(exc)
                msgs.append((i.get("name", ""), ok, msg))
            failed = [(n, m) for n, ok, m in msgs if not ok]
            return {"done": len(msgs) - len(failed), "failed": failed}

        def done(r):
            text = f"已启动 {r.get('done', 0)} 个卸载程序。"
            if r.get("failed"):
                text += "\n\n以下未能启动：\n" + "\n".join(
                    f"· {n}：{m}" for n, m in r["failed"][:8])
            messagebox.showinfo("卸载", text)
            self.scan_soft_all()

        self._clean_async(work, "启动卸载程序", done)

    # ---- 文件查找（顶层页签） ----

    def search_load_drives(self) -> None:
        try:
            values = [""] + [d["value"] for d in software.list_search_drives()]
        except Exception:
            values = [""]
        self.fs_drive_values = values
        combo = getattr(self, "fs_drive_combo", None)
        if combo is not None and combo.winfo_exists():
            if (combo["values"] or ()) == tuple(values):
                return
            combo["values"] = values
            combo.current(0)

    def do_search_files(self) -> None:
        kw = (self.fs_key_var.get() or "").strip()
        if not kw:
            messagebox.showinfo("文件查找", "请输入要查找的文件或文件夹名称关键字。")
            return
        combo = getattr(self, "fs_drive_combo", None)
        drive = ""
        if combo is not None:
            i = combo.current()
            vals = getattr(self, "fs_drive_values", [""]) or [""]
            drive = vals[i] if 0 <= i < len(vals) else ""
        root = (self.fs_root_var.get() or "").strip()
        mode = FS_MODE_MAP.get(self.fs_mode_var.get() or "文件 + 文件夹", "all")
        try:
            secs = max(3.0, min(float(self.fs_seconds_var.get() or 20), 120.0))
        except Exception:
            secs = 20.0
        try:
            depth = max(1, min(int(self.fs_depth_var.get() or 7), 16))
        except Exception:
            depth = 7
        try:
            min_mb = max(0.0, float(self.fs_minmb_var.get() or 0))
        except Exception:
            min_mb = 0.0
        ext = (self.fs_ext_var.get() or "").strip()
        sort_by = getattr(self, "fs_sort", "size")
        order = getattr(self, "fs_order", "desc")
        with_size = bool(self.fs_dirsize_var.get())

        def work():
            return software.search_files(keyword=kw, drive=drive, root=root, mode=mode,
                                         ext=ext, min_size_mb=min_mb, sort_by=sort_by,
                                         order=order, limit=500, max_seconds=secs,
                                         max_depth=depth, with_dir_size=with_size)

        def done(r):
            self.fs_rows = r.get("rows") or []
            self.fs_hint = r.get("hint") or ""
            self.fs_summary_var.set(
                f"关键字「{r.get('keyword', kw)}」：找到 {r.get('total_found', 0)} 项"
                f"（显示 {r.get('shown', 0)} 项） · 合计 {r.get('measured_text', '0 B')}"
                f" · 遍历 {r.get('scanned_dirs', 0)} 个目录 / "
                f"{r.get('scanned_files', 0)} 个文件 · 用时 {r.get('elapsed', 0)}s"
                + (" · 已触发时间上限" if r.get("timed_out") else ""))
            self.render_search_rows()
            self.status_var.set(f"文件查找完成：{r.get('total_found', 0)} 项")

        self._clean_async(work, "文件查找", done)

    def render_search_rows(self) -> None:
        tree = self.fs_tree
        if tree is None:
            return
        tree.delete(*tree.get_children())
        rows = self.fs_rows or []
        if not rows and self.fs_hint:
            tree.insert("", "end", values=("[ ]", "", self.fs_hint, "", "", "", ""))
        for i, r in enumerate(rows):
            tree.insert("", "end", iid=f"fsr{i}", values=(
                "[√]" if r.get("_checked") else "[ ]",
                "文件夹" if r.get("is_dir") else "文件",
                clip(r.get("name", ""), 28),
                r.get("parent", ""),
                r.get("size_text", "—"),
                r.get("mtime", ""),
                r.get("path", "")),
                tags=(("striped",) if i % 2 else ()))
        self.update_search_total()

    def toggle_search_row(self, event=None) -> None:
        tree = self.fs_tree
        if tree is None:
            return
        iid = tree.identify_row(event.y) if event else None
        if not iid:
            sel = tree.selection()
            iid = sel[0] if sel else None
        if not iid or not iid.startswith("fsr"):
            return
        idx = int(iid[3:])
        if idx >= len(self.fs_rows):
            return
        r = self.fs_rows[idx]
        r["_checked"] = not r.get("_checked")
        tree.set(iid, "sel", "[√]" if r["_checked"] else "[ ]")
        self.update_search_total()

    def check_all_search_rows(self) -> None:
        if not self.fs_rows:
            messagebox.showinfo("文件查找", "请先执行一次搜索。")
            return
        turn_on = any(not r.get("_checked") for r in self.fs_rows)
        for r in self.fs_rows:
            r["_checked"] = turn_on
        self.render_search_rows()

    def update_search_total(self) -> None:
        picked = [r for r in (self.fs_rows or []) if r.get("_checked")]
        dirs = sum(1 for r in picked if r.get("is_dir"))
        total = sum(int(r.get("size", 0) or 0) for r in picked)
        self.fs_total_var.set(
            f"已选 {len(picked)} 项（文件夹 {dirs} · 文件 {len(picked) - dirs}） · "
            f"合计 {cleanup.human_bytes(total)}")

    def sort_search(self, by: str) -> None:
        if getattr(self, "fs_sort", "size") == by:
            self.fs_order = "asc" if getattr(self, "fs_order", "desc") == "desc" else "desc"
        else:
            self.fs_sort = by
            self.fs_order = "desc" if by != "name" else "asc"
        self.update_search_sort_btn()
        if (self.fs_key_var.get() or "").strip():
            self.do_search_files()

    def update_search_sort_btn(self) -> None:
        order = getattr(self, "fs_order", "desc")
        sort = getattr(self, "fs_sort", "size")
        labels = {"size": "按大小", "time": "按时间", "name": "按名称"}
        for attr, by in (("fs_btn_size", "size"), ("fs_btn_time", "time"),
                         ("fs_btn_name", "name")):
            btn = getattr(self, attr, None)
            if btn is None:
                continue
            tail = (" ▼" if order == "desc" else " ▲") if sort == by else ""
            btn.configure(text=labels[by] + tail)

    def measure_search_selected(self) -> None:
        picked = [r for r in (self.fs_rows or []) if r.get("_checked")]
        if not picked:
            messagebox.showinfo("文件查找", "请先双击勾选要测算的结果行。")
            return
        targets = [r["path"] for r in picked][:40]

        def work():
            return software.measure_paths(targets, max_seconds=25.0)

        def done(r):
            mapping = {i["path"]: i for i in (r.get("items") or [])}
            for row in self.fs_rows or []:
                info = mapping.get(row["path"])
                if info and info.get("size", -1) >= 0:
                    row["size"] = info["size"]
                    row["size_text"] = info["size_text"]
            self.render_search_rows()
            self.status_var.set(f"已测算 {len(targets)} 项占用，合计 {r.get('total_text', '0 B')}")

        self._clean_async(work, "测算占用", done)

    def reveal_search_selected(self) -> None:
        tree = self.fs_tree
        path = ""
        if tree is not None:
            sel = tree.selection()
            if sel and sel[0].startswith("fsr"):
                idx = int(sel[0][3:])
                if idx < len(self.fs_rows):
                    path = self.fs_rows[idx]["path"]
        if not path:
            picked = [r for r in (self.fs_rows or []) if r.get("_checked")]
            path = picked[0]["path"] if picked else ""
        if not path:
            messagebox.showinfo("文件查找", "请先选中一行（或勾选一行）。")
            return
        ok, msg = software.reveal_path(path)
        self.status_var.set(msg)
        if not ok:
            messagebox.showwarning("文件查找", msg)

    def delete_search_selected(self) -> None:
        picked = [r for r in (self.fs_rows or []) if r.get("_checked")]
        if not picked:
            messagebox.showinfo("文件查找", "请先双击勾选要删除的结果行。")
            return
        total = sum(int(r.get("size", 0) or 0) for r in picked)
        if not messagebox.askyesno(
                "确认删除",
                f"即将把 {len(picked)} 项送入回收站：\n\n"
                + "\n".join("· " + r["name"] for r in picked[:10])
                + (f"\n…以及另外 {len(picked) - 10} 项" if len(picked) > 10 else "")
                + (f"\n\n合计 {cleanup.human_bytes(total)}。" if total else "")
                + "\n\n受系统保护的目录会被自动跳过，删除后可随时从回收站还原。"):
            return

        def work():
            return cleanup.delete_files([r["path"] for r in picked], use_recycle=True)

        def done(r):
            if isinstance(r, Exception):
                messagebox.showerror("删除失败", str(r))
                return
            _ok, info = r
            messagebox.showinfo(
                "删除完成",
                f"已删除 {info.get('deleted', 0)} 项，释放 {info.get('freed_text', '0 B')}；"
                f"跳过 {info.get('skipped', 0)} 个受保护文件，失败 {info.get('failed', 0)} 个。")
            self.do_search_files()

        self._clean_async(work, "删除搜索结果", done)

    # ---- 文件查找页签界面 ----

    def _build_search_tab(self, frame: ttk.Frame) -> None:
        self.fs_tree = None
        card = ttk.LabelFrame(frame, text="文件查找 · 文件与文件夹", padding=(14, 12))
        card.pack(fill="both", expand=True)

        bar = tk.Frame(card, bg=CARD)
        bar.pack(fill="x")
        ttk.Label(bar, text="分区：", style="CardMuted.TLabel").pack(side="left")
        self.fs_drive_combo = ttk.Combobox(bar, state="readonly", width=16,
                                           textvariable=self.fs_drive_var)
        self.fs_drive_combo.pack(side="left")
        ttk.Label(bar, text="起始目录：", style="CardMuted.TLabel").pack(side="left", padx=(14, 0))
        ttk.Entry(bar, width=24, textvariable=self.fs_root_var).pack(side="left")
        ttk.Label(bar, text="关键字：", style="CardMuted.TLabel").pack(side="left", padx=(14, 0))
        key_entry = ttk.Entry(bar, width=28, textvariable=self.fs_key_var)
        key_entry.pack(side="left")
        key_entry.bind("<Return>", lambda e: self.do_search_files())
        ttk.Label(bar, text="类型：", style="CardMuted.TLabel").pack(side="left", padx=(14, 0))
        mode_combo = ttk.Combobox(bar, state="readonly", width=12,
                                  values=tuple(FS_MODE_MAP.keys()),
                                  textvariable=self.fs_mode_var)
        mode_combo.current(0)
        mode_combo.pack(side="left")
        ttk.Label(bar, text="限时：", style="CardMuted.TLabel").pack(side="left", padx=(14, 0))
        ttk.Combobox(bar, state="readonly", width=6, values=("10", "20", "45", "90"),
                     textvariable=self.fs_seconds_var).pack(side="left")
        ttk.Button(bar, text="搜索", style="Primary.TButton",
                   command=self.do_search_files).pack(side="right")

        bar2 = tk.Frame(card, bg=CARD)
        bar2.pack(fill="x", pady=(8, 0))
        ttk.Label(bar2, text="扩展名：", style="CardMuted.TLabel").pack(side="left")
        ttk.Entry(bar2, width=10, textvariable=self.fs_ext_var).pack(side="left")
        ttk.Label(bar2, text="大小 ≥", style="CardMuted.TLabel").pack(side="left", padx=(14, 0))
        ttk.Entry(bar2, width=6, textvariable=self.fs_minmb_var).pack(side="left")
        ttk.Label(bar2, text="MB", style="CardMuted.TLabel").pack(side="left", padx=(3, 0))
        ttk.Label(bar2, text="深度：", style="CardMuted.TLabel").pack(side="left", padx=(14, 0))
        ttk.Combobox(bar2, state="readonly", width=5, values=("5", "7", "10"),
                     textvariable=self.fs_depth_var).pack(side="left")
        ttk.Checkbutton(bar2, text="统计文件夹占用", style="Card.TCheckbutton",
                        variable=self.fs_dirsize_var).pack(side="left", padx=(14, 0))
        ttk.Button(bar2, text="删除选中（送回收站）", style="Danger.TButton",
                   command=self.delete_search_selected).pack(side="right")
        ttk.Button(bar2, text="打开位置",
                   command=self.reveal_search_selected).pack(side="right", padx=(0, 8))
        ttk.Button(bar2, text="测算占用",
                   command=self.measure_search_selected).pack(side="right", padx=(0, 8))
        ttk.Button(bar2, text="全选",
                   command=self.check_all_search_rows).pack(side="right", padx=(0, 8))
        self.fs_btn_name = ttk.Button(bar2, text="按名称",
                                      command=lambda: self.sort_search("name"))
        self.fs_btn_name.pack(side="right", padx=(0, 8))
        self.fs_btn_time = ttk.Button(bar2, text="按时间",
                                      command=lambda: self.sort_search("time"))
        self.fs_btn_time.pack(side="right", padx=(0, 8))
        self.fs_btn_size = ttk.Button(bar2, text="按大小 ▼",
                                      command=lambda: self.sort_search("size"))
        self.fs_btn_size.pack(side="right", padx=(0, 8))

        ttk.Label(card, textvariable=self.fs_summary_var, style="CardMuted.TLabel",
                  wraplength=1100).pack(anchor="w", pady=(6, 0))

        f_cols = ("sel", "kind", "name", "parent", "size", "mtime", "path")
        self.fs_tree = ttk.Treeview(card, columns=f_cols, show="headings",
                                    height=16, style="Plain.Treeview")
        self.fs_tree.tag_configure("striped", background="#fafbfe")
        for cid, title, width, anchor in (("sel", "选中", 48, "center"),
                                          ("kind", "类型", 76, "center"),
                                          ("name", "名称", 200, "w"),
                                          ("parent", "所在目录", 300, "w"),
                                          ("size", "大小", 96, "e"),
                                          ("mtime", "修改时间", 140, "center"),
                                          ("path", "完整路径", 480, "w")):
            self.fs_tree.heading(cid, text=title)
            self.fs_tree.column(cid, width=width, anchor=anchor,
                                stretch=(cid in ("name", "path")))
        self.fs_tree.heading("size", command=lambda: self.sort_search("size"))
        self.fs_tree.heading("mtime", command=lambda: self.sort_search("time"))
        self.fs_tree.heading("name", command=lambda: self.sort_search("name"))
        f_vsb = ttk.Scrollbar(card, orient="vertical", command=self.fs_tree.yview)
        f_hsb = ttk.Scrollbar(card, orient="horizontal", command=self.fs_tree.xview)
        self.fs_tree.configure(yscrollcommand=f_vsb.set, xscrollcommand=f_hsb.set)
        f_pane = tk.Frame(card, bg=CARD)
        f_pane.pack(fill="both", expand=True, pady=(8, 0))
        self.fs_tree.grid(in_=f_pane, row=0, column=0, sticky="nsew")
        f_vsb.grid(in_=f_pane, row=0, column=1, sticky="ns")
        f_hsb.grid(in_=f_pane, row=1, column=0, sticky="ew")
        f_pane.grid_rowconfigure(0, weight=1)
        f_pane.grid_columnconfigure(0, weight=1)
        self.fs_tree.bind("<Double-1>", self.toggle_search_row)
        self.update_search_sort_btn()

        tip = ttk.Label(frame, style="Muted.TLabel", wraplength=1200, text=(
            "多个关键字用空格分隔（全部命中才算匹配）；限定「起始目录」可大幅提速。"
            "默认不实时统计文件夹体积，需要时勾选「统计文件夹占用」或对结果点「测算占用」。"
            "系统目录与依赖目录会自动跳过，删除一律送入回收站。"))
        tip.pack(anchor="w", pady=(8, 0))

    # ---------------- 数据 ----------------

    def refresh(self, force: bool = True, quick: bool = False) -> None:
        if self.busy:
            return
        self.busy = True
        self.status_var.set("正在扫描…")
        self.progress.start(12)
        self.elevate_btn.state(["disabled"])

        def work():
            try:
                data = scanner.full_scan(force=force, quick=quick)
                err = ""
            except Exception as exc:
                data, err = None, str(exc)
            self._ui(lambda: self._on_data(data, err))

        threading.Thread(target=work, daemon=True).start()

    def _on_data(self, data, err: str) -> None:
        self.busy = False
        self.progress.stop()
        if err or not data:
            self.status_var.set(f"扫描失败：{err}")
            return
        self.data = data
        self._render()
        s = data["summary"]
        self.stat_vars["running"].set(str(s["services_running"]))
        self.stat_vars["total"].set(str(s["services_total"]))
        self.stat_vars["proc"].set(str(s["processes_total"]))
        self.stat_vars["cpu"].set(f"{s['cpu_percent']}% / {s['memory_percent']}%")
        self.stat_vars["listen"].set(str(s["ports_listen"]))
        self.stat_vars["conn"].set(str(s["ports_total"]))
        admin = control.is_admin()
        self.admin_label.configure(
            text="✓ 管理员权限" if admin else "⚠ 非管理员权限",
            foreground="#16a34a" if admin else "#ea580c")
        if admin:
            self.elevate_btn.state(["!disabled", "disabled"])
        else:
            self.elevate_btn.state(["!disabled"])
        self.status_var.set(
            f"扫描完成 · 耗时 {data['elapsed']}s · {data['scan_time']} · 数据源 {data['source']}")

    def _apply_auto(self) -> None:
        if self._after_id:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        if self.auto_var.get():
            self._tick()

    def _tick(self) -> None:
        # 系统监控 / 清理 页签不参与扫描类自动刷新
        if self.auto_var.get() and not self.busy and self.tab not in ("monitor", "cleanup"):
            self.refresh(force=True, quick=True)
        self._after_id = self.root.after(self.interval_var.get() * 1000, self._tick)

    def _on_tab_changed(self, event=None) -> None:
        idx = self.notebook.index(self.notebook.select())
        self.tab = TAB_ORDER[idx]
        self._paint_tabs()

        self._toggle_monitor_timer(self.tab == "monitor")

        if self.tab in ("monitor", "cleanup", "search"):
            if self.tab == "monitor":
                self._refresh_monitor()
            elif self.tab == "search":
                self.search_load_drives()
            elif not self.clean_targets:
                self.scan_clean_targets()
            if self.tab == "cleanup":
                # 软件清理默认自动扫描本机全部软件（未扫过时）
                self.root.after(400, self._auto_scan_soft)
            return

        self.filter_combo["values"] = [t for _, t in FILTERS[self.tab]]
        self.filter_combo.current(0)
        self.filter_var.set("all")
        self._render()

    # ---------------- 渲染 ----------------

    def _rows_for_tab(self) -> list[dict]:
        if not self.data:
            return []
        tab = self.tab
        if tab == "running":
            rows = [r for r in self.data["services"]
                    if r["status"] == "running" or "pending" in str(r["status"])]
        elif tab == "services":
            rows = list(self.data["services"])
        elif tab == "processes":
            rows = list(self.data["processes"])
        else:
            rows = list(self.data["ports"])

        flt = self.filter_var.get()
        if flt != "all":
            label_to_value = {t: v for v, t in FILTERS[tab]}
            flt = label_to_value.get(flt, flt)
            if flt == "disabled":
                rows = [r for r in rows if r.get("start_type") == "disabled"]
            elif flt == "with_port":
                rows = [r for r in rows if r.get("ports")]
            elif flt == "high_mem":
                rows = [r for r in rows if (r.get("memory_mb") or 0) > 100]
            elif flt == "listen":
                rows = [r for r in rows if r.get("state") in ("LISTEN", "LISTENING")]
            elif flt == "tcp":
                rows = [r for r in rows if str(r.get("proto", "")).startswith("TCP")]
            elif flt == "udp":
                rows = [r for r in rows if str(r.get("proto", "")).startswith("UDP")]
            else:
                rows = [r for r in rows if r.get("status") == flt]

        kw = self.search_var.get().strip().lower()
        if kw:
            rows = [r for r in rows
                    if kw in " ".join(str(v) for v in r.values()).lower()]

        key, desc = self.sort.get(tab, ("", False))
        if key:
            rows.sort(key=lambda r: self._sort_value(r, key), reverse=desc)
        return rows

    @staticmethod
    def _sort_value(row: dict, key: str):
        v = row.get(key)
        if isinstance(v, list):
            v = v[0] if v else None
        if key in NUMERIC_KEYS:
            try:
                return float(v)
            except (TypeError, ValueError):
                return -1.0
        return str(v if v is not None else "")

    def _sort_by(self, key: str) -> None:
        cur_key, desc = self.sort.get(self.tab, ("", False))
        self.sort[self.tab] = (key, (not desc) if cur_key == key else False)
        self._render()

    def _render(self) -> None:
        # 系统监控 / 清理 两个页签不是表格，交给各自的渲染函数
        if self.tab not in self.trees:
            return
        tree = self.trees[self.tab]
        tree.delete(*tree.get_children())
        self.rows.clear()
        rows = self._rows_for_tab()
        for idx, row in enumerate(rows[:MAX_ROWS]):
            keys = self.visible_cols.get(
                self.tab, [c[0] for c in TAB_COLUMNS[self.tab]])
            values = [fmt_cell(row, k) for k in keys]
            status = str(row.get("status") or row.get("state") or "")
            tag = "running" if status in ("running", "LISTEN", "LISTENING") \
                else "pending" if "pending" in status else "stopped"
            # 斑马纹与状态色叠加：一个行可以同时拥有多个 tag，属性互不覆盖
            tags = (tag,) + (("striped",) if idx % 2 else ())
            iid = tree.insert("", "end", values=values, tags=tags)
            self.rows[iid] = row
        total = len(rows)
        self.count_var.set(
            f"共 {total} 条" if total <= MAX_ROWS
            else f"共 {total} 条，显示前 {MAX_ROWS} 条")
        self._layout_ops_labels(self.tab)
        self.root.after_idle(lambda: self._layout_ops_labels(self.tab))

    def _selected(self) -> dict | None:
        tree = self.trees.get(self.tab)
        if tree is None:
            return None
        sel = tree.selection()
        return self.rows.get(sel[0]) if sel else None

    # ---------------- 操作 ----------------

    def _async(self, fn, title: str) -> None:
        if self.busy:
            return
        self.busy = True
        self.progress.start(12)
        self.status_var.set(f"正在{title}…")

        def work():
            try:
                ok, msg = fn()
            except Exception as exc:
                ok, msg = False, str(exc)
            self._ui(lambda: self._done(ok, msg, title))

        threading.Thread(target=work, daemon=True).start()

    def _done(self, ok: bool, msg: str, title: str) -> None:
        self.busy = False
        self.progress.stop()
        self.status_var.set(f"{title}{'成功' if ok else '失败'}：{msg}")
        if not ok:
            messagebox.showerror(title, msg, parent=self.root)
        self.root.after(600, lambda: self.refresh(force=True, quick=True))

    def _target_service(self):
        """返回当前选中行对应的服务（进程/端口视图会按其 PID 反查承载的服务）。"""
        row = self._selected()
        if not row:
            messagebox.showinfo("提示", "请先选中一行", parent=self.root)
            return None, None
        if self.tab in ("running", "services"):
            return row, row
        pid = row.get("pid")
        svc = None
        for s in (self.data or {}).get("services", []):
            if s.get("pid") and s["pid"] == pid and s.get("status") == "running":
                svc = s
                break
        if not svc:
            messagebox.showinfo(
                "提示", f"进程 {row.get('name') or row.get('process_name')} (PID {pid}) "
                        f"没有承载任何运行中的 Windows 服务，无法执行服务操作。", parent=self.root)
            return None, None
        return svc, row

    def service_action(self, action: str) -> None:
        svc, _ = self._target_service()
        if not svc:
            return
        label = {"start": "启动", "stop": "停止", "restart": "重启"}[action]
        name = svc.get("display_name") or svc.get("name")
        if action in ("stop", "restart") and not messagebox.askyesno(
                "确认", f"确定{label}服务「{name}」吗？", parent=self.root):
            return
        svc_name = svc["name"]
        self._async(lambda: control.service_action(svc_name, action), f"{label}服务")

    def set_start_type(self, value: str) -> None:
        row = self._selected()
        if not row or self.tab not in ("running", "services"):
            messagebox.showinfo("提示", "请先在服务视图中选中一行", parent=self.root)
            return
        name = row["name"]
        self._async(lambda: control.set_start_type(name, value), "修改启动类型")

    def kill(self, tree_kill: bool) -> None:
        row = self._selected()
        if not row:
            messagebox.showinfo("提示", "请先选中一行", parent=self.root)
            return
        pid = row.get("pid")
        if not pid:
            messagebox.showinfo("提示", "该行没有对应的进程", parent=self.root)
            return
        pname = row.get("name") or row.get("process_name") or str(pid)
        tip = f"确定结束「{pname}」(PID {pid}) 及其子进程吗？" if tree_kill \
            else f"确定结束进程「{pname}」(PID {pid}) 吗？未保存的数据可能丢失。"
        if not messagebox.askyesno("确认", tip, parent=self.root):
            return
        self._async(lambda: control.kill_process(pid, tree=tree_kill),
                    "结束进程树" if tree_kill else "结束进程")

    def do_elevate(self) -> None:
        ok, msg = control.elevate(None)
        if not ok:
            messagebox.showinfo("提示", msg, parent=self.root)
        else:
            self.status_var.set(msg)

    def show_detail(self) -> None:
        row = self._selected()
        if not row:
            messagebox.showinfo("提示", "请先选中一行", parent=self.root)
            return
        win = tk.Toplevel(self.root)
        win.title("详细信息")
        win.geometry("780x540")
        win.configure(background=BG)
        box = self._card(win)
        box.pack(fill="both", expand=True, padx=14, pady=(14, 0))
        text = tk.Text(box, wrap="word", font=("Consolas", 10), padx=14, pady=12,
                       bd=0, highlightthickness=0, background=CARD, foreground=TEXT)
        text.pack(fill="both", expand=True)
        for k, v in row.items():
            if isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            text.insert("end", f"{k:<16}: {v}\n")
        text.configure(state="disabled")
        ttk.Button(win, text="关闭", command=win.destroy).pack(pady=12)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def run_smoke_test() -> int:
    """无窗口自检：验证 tkinter 可用 + 扫描链路正常（供打包后自动验证）。

    桌面版 exe 是无控制台的窗口程序，stdout 可能为 None，
    因此结果同时打印到控制台并写入临时日志文件。
    """
    import tempfile

    textutil.fix_console_encoding()
    lines: list[str] = []
    log_path = os.path.join(tempfile.gettempdir(), "winproc_manager_smoke.log")

    def emit(msg: str = "") -> None:
        lines.append(msg)
        try:
            print(msg)
        except Exception:
            pass

    code = 0

    def fail(msg: str) -> None:
        nonlocal code
        emit(msg)
        code = 1

    emit("=" * 56)
    emit("  Windows进程管理工具（潍鲸 - weijing.co） - 桌面版自检")
    emit("=" * 56)

    # 1) tkinter 可用性
    try:
        probe = tk.Tk()
        patch = probe.tk.call("info", "patchlevel")
        probe.destroy()
        emit(f"  tkinter        : OK (Tcl/Tk {patch})")
    except Exception as exc:
        fail(f"  tkinter        : 失败 -> {exc}")

    # 2) 主窗口能否实例化（含监控 / 清理两个新页签）
    if code == 0:
        try:
            monitor.start()
            hot = tk.Tk()
            hot.withdraw()
            app = App(hot)
            tabs = [app.notebook.tab(i, "text") for i in range(app.notebook.index("end"))]
            for i, name in enumerate(("running", "processes")):
                app.notebook.select(i)
                hot.update()
            app.notebook.select(4)
            hot.update()
            app.notebook.select(5)
            hot.update()
            hot.quit()
            hot.destroy()
            emit(f"  主界面         : OK  页签 {len(tabs)} 个 -> {' / '.join(tabs)}")
        except Exception as exc:
            fail(f"  主界面         : 失败 -> {exc}")

    # 3) 系统监控采样
    if code == 0:
        try:
            snap = monitor.snapshot(include_history=True)
            emit(f"  系统监控       : OK  CPU {snap['cpu']['percent']}%  "
                 f"内存 {snap['memory']['percent']}%  磁盘 {len(snap['disk']['partitions'])} 个  "
                 f"历史点 {snap['history_len']}")
        except Exception as exc:
            fail(f"  系统监控       : 失败 -> {exc}")

    # 4) 清理能力自检
    if code == 0:
        try:
            targets = cleanup.scan_targets()
            emit(f"  清理项扫描     : OK  {len(targets['rows'])} 项，可释放 {targets['total_text']}")
            ok, dry = cleanup.clean_disk(["thumbnails"], dry_run=True)
            emit(f"  清理预演       : OK  {dry['message']}（dry_run={dry['dry_run']}）")
            _ok, guard = cleanup.delete_files([r"C:\Windows\System32\notepad.exe"])
            emit(f"  删除护栏       : OK  跳过 {guard['skipped']} 个受保护路径")
        except Exception as exc:
            fail(f"  清理           : 失败 -> {exc}")

    if code == 0:
        try:
            data = scanner.full_scan(force=True)
            s = data["summary"]
            emit(f"  扫描           : OK  耗时 {data['elapsed']}s  数据源 {data['source']}")
            emit(f"  服务           : {s['services_total']}（运行中 {s['services_running']}）")
            emit(f"  进程 / 端口    : {s['processes_total']} / {s['ports_total']}")
            names = [x["display_name"] for x in data["services"]]
            cn = sum(1 for n in names if any("\u4e00" <= c <= "\u9fff" for c in n))
            bad = sum(1 for n in names if chr(0xFFFD) in n)
            emit(f"  中文显示名     : {cn} 个（乱码检测：{'通过' if bad == 0 else f'发现 {bad} 处乱码'}）")
        except Exception as exc:
            emit(f"  扫描           : 失败 -> {exc}")
            code = 1

    emit(f"  管理员权限     : {'是' if control.is_admin() else '否'}")
    emit("  自检通过" if code == 0 else "  自检失败")
    emit(f"  日志文件       : {log_path}")
    try:
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except Exception:
        pass
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="Windows进程管理工具（潍鲸 - weijing.co）（桌面版）")
    parser.add_argument("--smoke-test", action="store_true", help="无窗口自检后退出")
    args = parser.parse_args()

    textutil.fix_console_encoding()
    if args.smoke_test:
        return run_smoke_test()

    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
