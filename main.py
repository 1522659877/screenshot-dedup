# -*- coding: utf-8 -*-
"""
扫图去重 v3 —— 界面
蓝色主题 / 分组预览 / 勾选式删除 / 回收站可恢复
"""
from __future__ import annotations

import base64
import io
import multiprocessing
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import dedup_core as DC
import dedup_ocr as DO

# ============================================================
# 日志 / 配置 / 单实例
# ============================================================
_APP_DATA = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ScreenshotDedupV2"
try:
    _APP_DATA.mkdir(parents=True, exist_ok=True)
except Exception:
    pass
_LOG_FILE = _APP_DATA / "error.log"
_CFG_FILE = _APP_DATA / "config.json"


def log_error(where: str, exc: BaseException | None = None) -> None:
    """异常写进 error.log，方便事后排查（窗口版看不到控制台输出）。"""
    import traceback
    try:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] {where}\n")
            if exc is not None:
                f.write("".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)))
    except Exception:
        pass


def load_last_folder() -> str:
    try:
        import json
        with open(_CFG_FILE, "r", encoding="utf-8") as f:
            return str(json.load(f).get("last_folder", "") or "")
    except Exception:
        return ""


def save_last_folder(folder: str) -> None:
    try:
        import json
        with open(_CFG_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_folder": folder}, f, ensure_ascii=False)
    except Exception:
        pass


def _focus_running_instance() -> bool:
    """已经开着窗口时，把它叫到最前面（而不是再开一个新实例）。"""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _l):
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.startswith("扫图去重"):
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(_cb, 0)
        if found:
            h = found[0]
            user32.ShowWindow(h, 9)          # SW_RESTORE
            user32.SetForegroundWindow(h)
            return True
    except Exception as e:
        log_error("focus_running_instance", e)
    return False


def acquire_single_instance():
    """Windows 命名互斥体：双击多次不会开出多个实例（一个实例要解压 200MB）。"""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.CreateMutexW(None, False, "Local\\SaoTuDedupV3")
        if k32.GetLastError() == 183:        # ERROR_ALREADY_EXISTS
            _focus_running_instance()
            return None
        return handle
    except Exception as e:
        log_error("acquire_single_instance", e)
        return True


# ============================================================
# 配色
# ============================================================
BG        = "#F2F5FA"
CARD      = "#FFFFFF"
PRIMARY   = "#1B6EF3"
PRIMARY_D = "#1557C4"
PRIMARY_L = "#E8F0FE"
TEXT      = "#1B2333"
TEXT_DIM  = "#6B7A90"
BORDER    = "#DCE4EF"
GREEN     = "#12A150"
GREEN_L   = "#E6F6EC"
RED       = "#E5484D"
RED_L     = "#FDECEC"
AMBER     = "#B7791F"
AMBER_L   = "#FDF5E6"
ROW_ALT   = "#F8FAFD"

THUMB_MAX = 260


# ============================================================
# 后台任务：OCR + 分组
# ============================================================
class ScanTask(threading.Thread):
    def __init__(self, folder, recursive, threshold, use_phash, workers,
                 out_q: queue.Queue):
        super().__init__(daemon=True)
        self.folder = folder
        self.recursive = recursive
        self.threshold = threshold
        self.use_phash = use_phash
        self.workers = workers
        self.q = out_q
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def _cancelled(self):
        return self._cancel.is_set()

    # 生成缩略图（放进 worker 线程，别阻塞 UI）
    @staticmethod
    def _thumb_b64(path, box=THUMB_MAX):
        try:
            from PIL import Image
            im = Image.open(path)
            im = im.convert("RGB")
            im.thumbnail((box, box))
            buf = io.BytesIO()
            im.save(buf, format="PNG", optimize=True)
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception:
            return ""

    def run(self):
        try:
            self._run()
        except Exception as e:
            self.q.put(("fatal", f"{type(e).__name__}: {e}"))

    def _run(self):
        t_start = time.time()
        self.q.put(("status", "正在查找图片…"))
        try:
            imgs = DO.find_images(self.folder, self.recursive)
        except Exception as e:
            self.q.put(("fatal", str(e)))
            return
        if not imgs:
            self.q.put(("done", DC.ScanResult(
                folder=self.folder, recursive=self.recursive, total_images=0,
                threshold=self.threshold, elapsed=time.time() - t_start)))
            return
        if self._cancelled():
            return

        self.q.put(("phase", ("ocr", len(imgs))))

        def prog(done, total, name, cached):
            self.q.put(("progress", (done, total, name, cached)))

        res = DO.scan_images(imgs, workers=self.workers, progress=prog,
                             cancel=self._cancelled)
        if self._cancelled():
            self.q.put(("status", "已取消"))
            return

        self.q.put(("status", "正在生成缩略图…"))
        items = []
        for idx, p in enumerate(imgs):
            if self._cancelled():
                return
            r = res.get(str(p)) or {}
            it = DC.Item(
                path=p, norm=r.get("norm", ""), text=r.get("text", ""),
                conf=r.get("conf", 0.0), n_boxes=r.get("n_boxes", 0),
                error=r.get("error", ""),
            )
            it.thumb = self._thumb_b64(p)
            items.append(it)
            if idx % 3 == 0:
                self.q.put(("progress2", (idx + 1, len(imgs), p.name)))

        if self.use_phash:
            self.q.put(("status", "正在计算画面指纹…"))
            for idx, it in enumerate(items):
                if self._cancelled():
                    return
                it.phash = DC.compute_phash(it.path)
                if idx % 10 == 0:
                    self.q.put(("progress2", (idx + 1, len(items), it.path.name)))

        self.q.put(("status", "正在分组…"))
        groups = DC.group_items(
            items, threshold=self.threshold, use_phash=self.use_phash,
            progress=lambda d, t, m: self.q.put(("status", m)),
            cancel=self._cancelled,
        )

        sr = DC.ScanResult(
            folder=self.folder, recursive=self.recursive, total_images=len(imgs),
            items=items, groups=groups,
            no_text=[it for it in items if not it.is_text],
            errors=[f"{it.path.name}: {it.error}" for it in items if it.error],
            threshold=self.threshold, use_phash=self.use_phash,
            elapsed=time.time() - t_start,
        )
        self.q.put(("done", sr))


class DeleteTask(threading.Thread):
    def __init__(self, files, out_q):
        super().__init__(daemon=True)
        self.files = files
        self.q = out_q

    def run(self):
        ok = fail = 0
        freed = 0
        errs = []
        total = len(self.files)
        for i, f in enumerate(self.files, 1):
            size = 0
            try:
                size = f.stat().st_size
            except OSError:
                pass
            good, msg = DC.delete_to_trash(f)
            if good:
                ok += 1
                freed += size
            else:
                fail += 1
                errs.append(f"{f.name}: {msg}")
            self.q.put(("dprog", (i, total, f.name)))
        self.q.put(("ddone", (ok, fail, freed, errs)))


# ============================================================
# 主界面
# ============================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("扫图去重 v3  ·  找出重复文字截图并清理")
        self.configure(bg=BG)
        self.minsize(880, 620)
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self._center(min(1240, sw - 40), min(820, sh - 80))

        self.q: queue.Queue = queue.Queue()
        self.task: ScanTask | None = None
        self.del_task: DeleteTask | None = None
        self.result: DC.ScanResult | None = None
        self.checked: dict[int, tk.BooleanVar] = {}
        self._thumb_cache: dict[str, tk.PhotoImage] = {}
        self._cur_thumb_ref = None
        self.current_item: DC.Item | None = None

        self.report_callback_exception = lambda t, v, tb: log_error(
            "tk 回调异常", v if isinstance(v, BaseException) else Exception(str(v)))

        self._init_style()
        self._build()
        self.after(80, self._pump)

    def _center(self, w, h):
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 3)
        self.geometry(f"{w}x{h}+{x}+{y}")

    # ---------------- 样式 ----------------
    def _init_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass

        st.configure(".", background=BG, foreground=TEXT,
                     font=("Microsoft YaHei UI", 10))
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD)
        st.configure("TLabel", background=BG, foreground=TEXT)
        st.configure("Card.TLabel", background=CARD, foreground=TEXT)
        st.configure("Dim.TLabel", background=CARD, foreground=TEXT_DIM,
                     font=("Microsoft YaHei UI", 9))
        st.configure("H1.TLabel", background=BG, foreground=TEXT,
                     font=("Microsoft YaHei UI", 16, "bold"))
        st.configure("H2.TLabel", background=CARD, foreground=TEXT,
                     font=("Microsoft YaHei UI", 11, "bold"))
        st.configure("Stat.TLabel", background=CARD, foreground=PRIMARY,
                     font=("Microsoft YaHei UI", 20, "bold"))
        st.configure("StatCap.TLabel", background=CARD, foreground=TEXT_DIM,
                     font=("Microsoft YaHei UI", 9))

        st.configure("TButton", background=CARD, foreground=TEXT,
                     bordercolor=BORDER, focuscolor=CARD,
                     font=("Microsoft YaHei UI", 10), padding=(12, 7))
        st.map("TButton",
               background=[("active", PRIMARY_L), ("disabled", CARD)],
               foreground=[("disabled", TEXT_DIM)])

        st.configure("Primary.TButton", background=PRIMARY, foreground="#FFFFFF",
                     bordercolor=PRIMARY, focuscolor=PRIMARY,
                     font=("Microsoft YaHei UI", 10, "bold"), padding=(16, 8))
        st.map("Primary.TButton",
               background=[("active", PRIMARY_D), ("disabled", "#A9C1E8")],
               foreground=[("disabled", "#FFFFFF")])

        st.configure("Danger.TButton", background=RED, foreground="#FFFFFF",
                     bordercolor=RED, focuscolor=RED,
                     font=("Microsoft YaHei UI", 10, "bold"), padding=(16, 8))
        st.map("Danger.TButton",
               background=[("active", "#C93A3E"), ("disabled", "#EBB4B6")],
               foreground=[("disabled", "#FFFFFF")])

        st.configure("TCheckbutton", background=CARD, foreground=TEXT,
                     focuscolor=CARD)
        st.map("TCheckbutton", background=[("active", CARD)])
        st.configure("Bg.TCheckbutton", background=BG, foreground=TEXT,
                     focuscolor=BG)
        st.map("Bg.TCheckbutton", background=[("active", BG)])

        st.configure("TEntry", fieldbackground=CARD, bordercolor=BORDER,
                     lightcolor=BORDER, darkcolor=BORDER, padding=6)


        st.configure("Thin.Horizontal.TProgressbar", troughcolor="#DCE4EF",
                     background=PRIMARY, bordercolor="#DCE4EF",
                     lightcolor=PRIMARY, darkcolor=PRIMARY, thickness=8)

        st.configure("Treeview", background=CARD, fieldbackground=CARD,
                     foreground=TEXT, rowheight=34, borderwidth=0,
                     font=("Microsoft YaHei UI", 10))
        st.configure("Treeview.Heading", background=PRIMARY_L,
                     foreground=PRIMARY_D, relief="flat",
                     font=("Microsoft YaHei UI", 10, "bold"), padding=(6, 6))
        st.map("Treeview.Heading", background=[("active", "#D8E4FB")])
        st.map("Treeview", background=[("selected", PRIMARY_L)],
               foreground=[("selected", TEXT)])

    # ---------------- 布局 ----------------
    def _build_dedup_tab(self, root):
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        # ---- 设置卡片 ----
        card = ttk.Frame(root, style="Card.TFrame", padding=14)
        card.grid(row=0, column=0, sticky="ew", pady=(12, 10))
        card.columnconfigure(1, weight=1)
        for c in range(6):
            card.columnconfigure(c, weight=1 if c == 1 else 0)

        ttk.Label(card, text="文件夹", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.var_folder = tk.StringVar(value="")
        ttk.Entry(card, textvariable=self.var_folder).grid(row=0, column=1, sticky="ew", padx=(0, 8))
        ttk.Button(card, text="浏览…", command=self._browse).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(card, text="用程序所在目录", command=self._use_here).grid(row=0, column=3)

        row2 = ttk.Frame(card, style="Card.TFrame")
        row2.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(12, 0))

        self.var_recursive = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="包含子文件夹", variable=self.var_recursive,
                        style="TCheckbutton").pack(side="left")

        self.var_phash = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="画面相似也合并（感知哈希）", variable=self.var_phash,
                        style="TCheckbutton").pack(side="left", padx=(16, 0))


        ttk.Label(row2, text="相似度阈值", style="Card.TLabel").pack(side="left", padx=(22, 6))
        self.var_th = tk.DoubleVar(value=0.85)
        self.scale = ttk.Scale(row2, from_=0.50, to=1.00, variable=self.var_th,
                               command=self._on_th, length=170)
        self.scale.pack(side="left")
        self.lbl_th = ttk.Label(row2, text="85%", style="Card.TLabel", width=5,
                                font=("Microsoft YaHei UI", 10, "bold"), foreground=PRIMARY)
        self.lbl_th.pack(side="left", padx=(4, 0))

        ttk.Label(row2, text="并行进程", style="Card.TLabel").pack(side="left", padx=(22, 6))
        self.var_workers = tk.IntVar(value=min(4, max(1, multiprocessing.cpu_count() - 1)))
        ttk.Spinbox(row2, from_=1, to=16, width=4, textvariable=self.var_workers).pack(side="left")

        # ---- 操作栏（拆成两行，保证"删除"永远可见）----
        bar = ttk.Frame(root)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        bar.columnconfigure(0, weight=1)

        row_a = ttk.Frame(bar)
        row_a.grid(row=0, column=0, sticky="ew")
        self.btn_scan = ttk.Button(row_a, text="①  开始扫描", style="Primary.TButton",
                                   command=self._start_scan)
        self.btn_scan.pack(side="left")
        self.btn_cancel = ttk.Button(row_a, text="取消", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=(6, 0))
        self.btn_delete = ttk.Button(row_a, text="②  删除选中项", style="Danger.TButton",
                                     command=self._delete, state="disabled")
        self.btn_delete.pack(side="left", padx=(14, 0))
        self.lbl_status = ttk.Label(row_a, text="就绪", style="TLabel", foreground=TEXT_DIM)
        self.lbl_status.pack(side="right", padx=(10, 0))

        row_b = ttk.Frame(bar)
        row_b.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        for txt, cmd in (("全部展开", lambda: self._expand(True)),
                         ("全部折叠", lambda: self._expand(False)),
                         ("全选待删", lambda: self._check(True)),
                         ("全不选", lambda: self._check(False))):
            b = ttk.Button(row_b, text=txt, command=cmd)
            b.pack(side="left", padx=(0, 6))
            if txt == "全部展开":
                self.btn_expand = b
            elif txt == "全部折叠":
                self.btn_collapse = b
            elif txt == "全选待删":
                self.btn_check_all = b
            else:
                self.btn_uncheck = b
        self.lbl_hint = ttk.Label(row_b, text="", style="TLabel", foreground=TEXT_DIM)
        self.lbl_hint.pack(side="right", padx=(10, 0))

        # ---- 进度 ----
        self.pb = ttk.Progressbar(root, style="Thin.Horizontal.TProgressbar",
                                  mode="determinate", maximum=100)
        self.pb.grid(row=3, column=0, sticky="ew", pady=(0, 10))

        # ---- 主体：左树 + 右预览 ----
        body = ttk.Frame(root)
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2, minsize=340)
        body.rowconfigure(0, weight=1)

        # 左：统计 + 树
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)

        stats = ttk.Frame(left, style="Card.TFrame", padding=12)
        stats.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        for i in range(4):
            stats.columnconfigure(i, weight=1)
        self.stat_labels = {}
        for i, (key, cap) in enumerate([("total", "已扫描图片"), ("groups", "重复组"),
                                        ("del", "待删张数"), ("size", "可释放空间")]):
            cell = ttk.Frame(stats, style="Card.TFrame")
            cell.grid(row=0, column=i, sticky="ew")
            val = ttk.Label(cell, text="—", style="Stat.TLabel")
            val.pack()
            ttk.Label(cell, text=cap, style="StatCap.TLabel").pack()
            self.stat_labels[key] = val

        wrap = ttk.Frame(left, style="Card.TFrame")
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        self.tree = ttk.Treeview(wrap, columns=("size", "sim", "type"),
                                 selectmode="browse")
        self.tree.heading("#0", text="文件 / 重复组", anchor="w")
        self.tree.heading("size", text="大小", anchor="e")
        self.tree.heading("sim", text="相似度", anchor="center")
        self.tree.heading("type", text="匹配方式", anchor="center")
        self.tree.column("#0", width=420, minwidth=240, stretch=True)
        self.tree.column("size", width=90, anchor="e", stretch=False)
        self.tree.column("sim", width=80, anchor="center", stretch=False)
        self.tree.column("type", width=96, anchor="center", stretch=False)

        vs = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        self.tree.tag_configure("keep", foreground=GREEN)
        self.tree.tag_configure("del", foreground=RED)
        self.tree.tag_configure("group", background=ROW_ALT,
                                font=("Microsoft YaHei UI", 10, "bold"))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<space>", self._on_space)
        self.tree.bind("<Double-1>", self._open_file)

        # 右：预览
        right = ttk.Frame(body, style="Card.TFrame", padding=12)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.rowconfigure(3, weight=1)
        right.columnconfigure(0, weight=1)

        ttk.Label(right, text="预览", style="H2.TLabel").grid(row=0, column=0, sticky="w")
        self.cv = tk.Canvas(right, bg="#EDF2F9", highlightthickness=1,
                            highlightbackground=BORDER)
        self.cv.grid(row=1, column=0, sticky="nsew", pady=(8, 10))

        self.lbl_preview = ttk.Label(right, text="选中左侧文件查看预览",
                                     style="Dim.TLabel", wraplength=320,
                                     justify="left")
        self.lbl_preview.grid(row=2, column=0, sticky="ew")

        box = ttk.Frame(right, style="Card.TFrame")
        box.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.txt = tk.Text(box, wrap="word", height=9, relief="flat",
                           bg="#F8FAFD", fg=TEXT, insertbackground=TEXT,
                           font=("Microsoft YaHei UI", 9), padx=10, pady=8,
                           highlightthickness=1, highlightbackground=BORDER)
        self.txt.grid(row=0, column=0, sticky="nsew")
        ts = ttk.Scrollbar(box, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=ts.set)
        ts.grid(row=0, column=1, sticky="ns")
        self.txt.configure(state="disabled")



    # ============================================================
    #  主窗口装配
    # ============================================================
    def _build(self):
        wrap = ttk.Frame(self, padding=(14, 12, 14, 10))
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(1, weight=1)

        head = ttk.Frame(wrap)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        head.columnconfigure(1, weight=1)
        ttk.Label(head, text="🐟  扫图去重 v3", style="H1.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(head, text="RapidOCR 引擎  ·  中文准确率 ~98%  ·  删除进回收站可恢复",
                  style="Dim.TLabel").grid(row=0, column=1, sticky="w", padx=(12, 0))

        self.tab_dedup = ttk.Frame(wrap)
        self.tab_dedup.grid(row=1, column=0, sticky="nsew")
        self._build_dedup_tab(self.tab_dedup)

    # ---------------- 交互 ----------------
    def _on_th(self, _v=None):
        self.lbl_th.config(text=f"{int(float(self.var_th.get()) * 100)}%")

    def _browse(self):
        d = filedialog.askdirectory(title="选择要扫描的文件夹")
        if d:
            self.var_folder.set(d)

    def _use_here(self):
        base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else HERE
        self.var_folder.set(base)

    def _set_busy(self, busy: bool):
        st = "disabled" if busy else "normal"
        for b in (self.btn_scan, self.btn_expand, self.btn_collapse,
                  self.btn_check_all, self.btn_uncheck):
            b.config(state=st)
        self.btn_scan.config(text="扫描中…" if busy else "①  开始扫描")
        self.btn_cancel.config(state="normal" if busy else "disabled")
        self.btn_delete.config(
            state="normal" if (not busy and self.result and self._checked_files()) else "disabled")

    def _start_scan(self):
        folder = self.var_folder.get().strip().strip('"')
        if not folder:
            messagebox.showwarning("还没选文件夹", "请先选择要扫描的文件夹。")
            return
        if not os.path.isdir(folder):
            messagebox.showerror("文件夹不存在", folder)
            return
        save_last_folder(folder)
        self.tree.delete(*self.tree.get_children())
        self.checked.clear()
        self._thumb_cache.clear()
        self.result = None
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")
        self.cv.delete("all")
        for k in self.stat_labels:
            self.stat_labels[k].config(text="—")
        self.pb.config(value=0)
        self._set_busy(True)

        self.task = ScanTask(folder, self.var_recursive.get(),
                             float(self.var_th.get()), self.var_phash.get(),
                             int(self.var_workers.get()), self.q)
        self.task.start()

    def _cancel(self):
        if self.task:
            self.task.cancel()
            self.lbl_status.config(text="正在取消…")

    def _check(self, on: bool):
        for v in self.checked.values():
            v.set(on)
        self._refresh_tree_tags()
        self.btn_delete.config(state="normal" if self._checked_files() and not (
            self.task and self.task.is_alive()) else "disabled")

    def _checked_files(self):
        if not self.result:
            return []
        out = []
        for gi, g in enumerate(self.result.groups):
            v = self.checked.get(gi)
            if v and v.get():
                out.extend(it.path for it in g.to_delete)
        return out

    def _refresh_tree_tags(self):
        for gi, g in enumerate(self.result.groups if self.result else []):
            v = self.checked.get(gi)
            if not v:
                continue
            iid = f"g{gi}"
            if self.tree.exists(iid):
                self.tree.item(iid, tags=("group", "del" if v.get() else "keep"))
                state = "☑" if v.get() else "☐"
                self.tree.item(iid, text=f"{state}  重复组 {gi + 1}   （{len(g.items)} 张，保留 1 张）")

    # ---------------- 事件队列 ----------------
    def _pump(self):
        # 注意：这里绝不能因为某条消息处理失败就停掉轮询，
        # 否则界面会永远停在“扫描中…”，按钮一直是灰的。
        try:
            while True:
                try:
                    kind, payload = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(kind, payload)
                except Exception as e:
                    log_error(f"处理消息 {kind} 失败", e)
        finally:
            self.after(80, self._pump)

    def _handle(self, kind, payload):
        if kind == "status":
            self.lbl_status.config(text=str(payload))
        elif kind == "phase":
            which, total = payload
            self.pb.config(maximum=max(1, total), value=0)
        elif kind == "progress":
            done, total, name, cached = payload
            self.pb.config(maximum=max(1, total), value=done)
            tag = "缓存命中" if cached else "识别中"
            self.lbl_status.config(text=f"OCR {done}/{total}  {tag}  {name}")
        elif kind == "progress2":
            done, total, name = payload
            self.pb.config(maximum=max(1, total), value=done)
            self.lbl_status.config(text=f"{done}/{total}  {name}")
        elif kind == "done":
            self._on_done(payload)
        elif kind == "fatal":
            self._set_busy(False)
            messagebox.showerror("出错了", str(payload))
            self.lbl_status.config(text="出错了")
        elif kind == "dprog":
            i, total, name = payload
            self.pb.config(maximum=max(1, total), value=i)
            self.lbl_status.config(text=f"删除 {i}/{total}  {name}")
        elif kind == "ddone":
            self._on_deleted(payload)

    def _on_done(self, sr: DC.ScanResult):
        self.result = sr
        self._set_busy(False)
        self.pb.config(value=self.pb["maximum"])

        self.stat_labels["total"].config(text=str(sr.total_images))
        self.stat_labels["groups"].config(text=str(len(sr.groups)))
        self.stat_labels["del"].config(text=str(sr.delete_count))
        self.stat_labels["size"].config(text=DC.human_size(sr.delete_bytes))

        note = []
        if sr.no_text:
            note.append(f"{len(sr.no_text)} 张无文字")
        if sr.errors:
            note.append(f"{len(sr.errors)} 张识别出错")
        self.lbl_status.config(
            text=f"完成，用时 {sr.elapsed:.1f} 秒" + ("（" + "、".join(note) + "）" if note else ""))

        self._fill_tree()
        if not sr.groups:
            self.lbl_preview.config(text="没有发现文字重复的截图 🎉")
        self.btn_delete.config(state="normal" if self._checked_files() else "disabled")


    def _fill_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.checked.clear()
        if not self.result:
            return
        for gi, g in enumerate(self.result.groups):
            var = tk.BooleanVar(value=True)
            self.checked[gi] = var
            iid = f"g{gi}"
            self.tree.insert(
                "", "end", iid=iid, open=True,
                text=f"☑  重复组 {gi + 1}   （{len(g.items)} 张，保留 1 张）",
                values=(DC.human_size(g.saved_bytes), f"{g.min_sim:.0%}", g.match_type),
                tags=("group", "del"),
            )
            k = g.keeper
            self.tree.insert(iid, "end", iid=f"{iid}_k", text=f"   ✅ 保留  {k.path.name}",
                             values=(k.size_h, "", ""), tags=("keep",))
            for it in g.to_delete:
                self.tree.insert(iid, "end", iid=f"{iid}_{it.path.name}",
                                 text=f"   🗑 删除  {it.path.name}",
                                 values=(it.size_h, "", ""), tags=("del",))

    def _expand(self, open_: bool):
        for iid in self.tree.get_children():
            self.tree.item(iid, open=open_)

    # ---------------- 选择 / 预览 ----------------
    def _on_space(self, _e):
        sel = self.tree.selection()
        if not sel:
            return "break"
        iid = sel[0]
        if iid.startswith("g") and "_" not in iid:
            gi = int(iid[1:])
            v = self.checked.get(gi)
            if v:
                v.set(not v.get())
                self._refresh_tree_tags()
                self.btn_delete.config(state="normal" if self._checked_files() else "disabled")
        return "break"

    def _on_select(self, _e=None):
        sel = self.tree.selection()
        if not sel or not self.result:
            return
        iid = sel[0]
        it = None
        if iid.endswith("_k"):
            gi = int(iid[1:].split("_")[0])
            it = self.result.groups[gi].keeper
        elif "_" in iid and iid.startswith("g"):
            parts = iid.split("_", 1)
            gi = int(parts[0][1:])
            name = parts[1]
            for cand in self.result.groups[gi].items:
                if cand.path.name == name:
                    it = cand
                    break
        if it is None:
            return
        self.current_item = it
        self._show_thumb(it)
        self._show_text(it)

    def _show_thumb(self, it: DC.Item):
        self.cv.delete("all")
        b64 = getattr(it, "thumb", "")
        if not b64:
            self.cv.create_text(10, 10, anchor="nw", fill=TEXT_DIM,
                                text="（无法生成预览）")
            return
        try:
            photo = tk.PhotoImage(data=b64)
        except Exception:
            self.cv.create_text(10, 10, anchor="nw", fill=TEXT_DIM, text="（预览失败）")
            return
        self._thumb_cache[it.path.name] = photo
        self._cur_thumb_ref = photo
        cw = max(self.cv.winfo_width(), 200)
        ch = max(self.cv.winfo_height(), 160)
        self.cv.create_image(cw // 2, ch // 2, image=photo)

    def _show_text(self, it: DC.Item):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        head = (f"文件：{it.path.name}\n"
                f"大小：{it.size_h}    文本块：{it.n_boxes}    "
                f"置信度：{it.conf:.3f}    字符数：{len(it.norm)}\n"
                f"{'-' * 46}\n")
        self.txt.insert("end", head)
        self.txt.insert("end", it.text or "（未识别到文字）")
        if it.error:
            self.txt.insert("end", f"\n\n[错误] {it.error}")
        self.txt.configure(state="disabled")
        self.lbl_preview.config(text=f"{it.path.name}   ·   {it.size_h}")
        try:
            self.title(f"扫图去重 v3  ·  {it.path.name}")
        except Exception:
            pass

    def _open_file(self, _e=None):
        if self.current_item and self.current_item.path.exists():
            try:
                os.startfile(str(self.current_item.path))
            except Exception:
                pass


    # ---------------- 删除 ----------------
    def _delete(self):
        files = self._checked_files()
        if not files:
            messagebox.showinfo("没有选中项", "请先勾选要删除的重复组。")
            return
        total = sum((f.stat().st_size if f.exists() else 0) for f in files)
        if not messagebox.askokcancel(
            "确认删除",
            f"即将把 {len(files)} 个文件移入回收站（可恢复）。\n\n"
            f"预计释放：{DC.human_size(total)}\n\n"
            f"每个重复组都会保留文件最大的那一张。\n继续？"
        ):
            return
        self.del_task = DeleteTask(files, self.q)
        self.del_task.start()
        self._set_busy(True)
        self.lbl_status.config(text="正在移入回收站…")

    def _on_deleted(self, payload):
        ok, fail, freed, errs = payload
        self._set_busy(False)
        self.lbl_status.config(
            text=f"已删除 {ok} 个文件，释放 {DC.human_size(freed)}" +
                 (f"，{fail} 个失败" if fail else ""))
        msg = f"成功移入回收站：{ok} 个\n释放空间：{DC.human_size(freed)}"
        if fail:
            msg += f"\n\n失败 {fail} 个：\n" + "\n".join(errs[:8])
        messagebox.showinfo("完成", msg)
        if self.result and self.var_folder.get().strip():
            if messagebox.askyesno("重新扫描", "是否重新扫描以刷新列表？"):
                self._start_scan()


def _selftest(app: "App", folder: str, out_txt: str):
    """
    隐藏自检模式（打包后验证 OCR 模型是否随包发布）：
      扫图去重v2.exe --selftest "文件夹" "输出txt"
    扫描完成后把结果写文件并退出，用于自动化验证。
    """
    import traceback
    state = {"phase": "start", "t0": time.time()}
    errs = []

    def report(exc, val, tb):
        errs.append("".join(traceback.format_exception(exc, val, tb)))
    app.report_callback_exception = report

    def tick():
        if time.time() - state["t0"] > 300:
            _write(f"TIMEOUT\n" + "\n".join(errs)); app.destroy(); return
        if state["phase"] == "start":
            app.var_folder.set(folder)
            app.var_workers.set(2)
            app._start_scan()
            state["phase"] = "wait"
            app.after(500, tick)
            return
        if app.task and app.task.is_alive():
            app.after(500, tick)
            return
        r = app.result
        if r is None:
            _write("RESULT=None\n" + "\n".join(errs))
        else:
            lines = [
                f"OK images={r.total_images} groups={len(r.groups)} "
                f"delete={r.delete_count} bytes={r.delete_bytes} elapsed={r.elapsed:.1f}",
            ]
            for gi, g in enumerate(r.groups, 1):
                lines.append(f"GROUP{gi} type={g.match_type} sim={g.min_sim:.3f} "
                             f"n={len(g.items)} keep={g.keeper.path.name} "
                             f"text={g.keeper.norm[:50]}")
                for it in g.to_delete:
                    lines.append(f"   DEL {it.path.name} {it.size_h}")
            ok = sum(1 for it in r.items if it.is_text)
            lines.append(f"TEXTDETECTED={ok}/{r.total_images}")
            if errs:
                lines.append("ERRORS:\n" + "\n".join(errs))
            _write("\n".join(lines))
        app.destroy()

    def _write(s):
        try:
            with open(out_txt, "w", encoding="utf-8") as f:
                f.write(s)
        except Exception:
            pass

    app.after(600, tick)


def main():
    multiprocessing.freeze_support()
    argv = sys.argv[1:]

    # 自检模式（去重）
    if len(argv) >= 3 and argv[0] == "--selftest":
        app = App()
        _selftest(app, argv[1], argv[2])
        app.mainloop()
        return

    guard = acquire_single_instance()
    if guard is None:          # 已经有窗口在跑，刚刚已把它叫到前面
        return

    app = App()

    # 启动时只“预填文件夹”，不自动开扫。
    # 旧版双击后会自动扫描 exe 所在目录，扫描期间“开始扫描”一直是灰的，
    # 看起来就像用不了 —— 现在按钮从一开始就是可点的。
    if len(argv) >= 1 and os.path.isdir(argv[0]):
        app.var_folder.set(argv[0])
    else:
        here = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else ""
        app.var_folder.set(load_last_folder() or here)
        app.lbl_hint.config(text="选好文件夹后点 ① 开始扫描")

    app.mainloop()


def _install_excepthook():
    def hook(t, v, tb):
        log_error("未捕获异常", v if isinstance(v, BaseException) else Exception(str(v)))
    sys.excepthook = hook
    try:
        threading.excepthook = lambda a: log_error(
            "线程异常", a.exc if a.exc else Exception(str(a)))
    except Exception:
        pass


if __name__ == "__main__":
    _install_excepthook()
    main()
