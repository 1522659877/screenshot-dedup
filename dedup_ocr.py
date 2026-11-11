# -*- coding: utf-8 -*-
"""
dedup_ocr.py —— OCR 引擎
用 RapidOCR（PP-OCRv4 ONNX）替代 Tesseract：
  - 中文准确率 ~98%，实测零乱码
  - 多进程并行 + 结果缓存
"""
from __future__ import annotations

import os
import hashlib
import json
import pickle
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}

# 每个 worker 进程各自持有一个 OCR 实例（RapidOCR 不是进程安全的共享对象）
_OCR = None


def _get_ocr():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    return _OCR


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ============================================================
# 缓存
# ============================================================
def default_cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = Path(base) / "ScreenshotDedupV2"
    d.mkdir(parents=True, exist_ok=True)
    return d


def file_key(path: Path) -> str:
    """缓存键：路径 + 大小 + 修改时间（内容变了就失效）"""
    try:
        st = path.stat()
        raw = f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8")
    except OSError:
        raw = str(path).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


# ============================================================
# 单张识别
# ============================================================
def ocr_one(path: str) -> dict:
    """
    识别一张图，返回:
      {ok, text, norm, conf, n_boxes, error}
    按 y 坐标分行、行内按 x 排序，拼成阅读顺序正确的文本
    """
    p = Path(path)
    try:
        ocr = _get_ocr()
        result, _elapse = ocr(str(p))
    except Exception as e:
        return {"ok": False, "text": "", "norm": "", "conf": 0.0,
                "n_boxes": 0, "error": f"{type(e).__name__}: {e}"}

    if not result:
        return {"ok": True, "text": "", "norm": "", "conf": 0.0,
                "n_boxes": 0, "error": ""}

    items = []
    for entry in result:
        try:
            box, text, score = entry[0], entry[1], entry[2]
        except Exception:
            continue
        if not text:
            continue
        ys = [pt[1] for pt in box]
        xs = [pt[0] for pt in box]
        items.append({
            "y": min(ys), "yc": sum(ys) / len(ys),
            "x": min(xs), "text": str(text), "score": float(score),
            "h": max(ys) - min(ys),
        })

    if not items:
        return {"ok": True, "text": "", "norm": "", "conf": 0.0,
                "n_boxes": 0, "error": ""}

    # 行分组：按框高度估算行距
    items.sort(key=lambda d: d["yc"])
    med_h = sorted(d["h"] for d in items)[len(items) // 2] or 1.0
    tolerance = max(med_h * 0.6, 6.0)

    lines, cur = [], [items[0]]
    for it in items[1:]:
        if abs(it["yc"] - cur[-1]["yc"]) <= tolerance:
            cur.append(it)
        else:
            lines.append(cur)
            cur = [it]
    lines.append(cur)

    parts = []
    for ln in lines:
        ln.sort(key=lambda d: d["x"])
        parts.append("".join(d["text"] for d in ln))

    text = "\n".join(parts)
    conf = sum(d["score"] for d in items) / len(items)
    return {"ok": True, "text": text, "norm": normalize_text(text),
            "conf": conf, "n_boxes": len(items), "error": ""}


# ============================================================
# 文本归一化
# ============================================================
import re

_FULLWIDTH = {0x3000: 0x20}
for _c in range(0xFF01, 0xFF5F):
    _FULLWIDTH[_c] = _c - 0xFEE0
_FW_TABLE = str.maketrans(_FULLWIDTH)

# 只保留中文、字母、数字
_KEEP = re.compile(r"[^\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9]")


def normalize_text(text: str) -> str:
    """归一化：全角→半角、小写、只留中英文数字。
    这样截图尺寸/配色/字体差异导致的空格、标点差异不会影响比对。"""
    if not text:
        return ""
    t = text.translate(_FW_TABLE).lower()
    return _KEEP.sub("", t)


def find_images(folder: str, recursive: bool = False) -> list[Path]:
    root = Path(folder)
    if not root.exists():
        raise FileNotFoundError(f"文件夹不存在: {folder}")
    if not root.is_dir():
        raise NotADirectoryError(f"不是文件夹: {folder}")
    it = root.rglob("*") if recursive else root.glob("*")
    out = [p for p in it if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
    return sorted(out)


# ============================================================
# 批量识别（多进程 + 缓存）
# ============================================================
def _worker(args):
    path, cache_dir = args
    p = Path(path)
    key = file_key(p)
    cf = Path(cache_dir) / f"{key}.pkl"
    if cf.exists():
        try:
            with open(cf, "rb") as f:
                return path, pickle.load(f), True
        except Exception:
            pass
    res = ocr_one(path)
    try:
        with open(cf, "wb") as f:
            pickle.dump(res, f, protocol=4)
    except Exception:
        pass
    return path, res, False


def scan_images(paths, workers: int = 4, cache_dir=None,
                progress=None, cancel=None):
    """
    并行 OCR 一批图片。
    progress(done, total, current_name, cached_hit)
    cancel() -> True 时中止
    返回 {path_str: result_dict}
    """
    cache_dir = str(cache_dir or default_cache_dir())
    total = len(paths)
    results: dict[str, dict] = {}
    if total == 0:
        return results

    workers = max(1, min(workers, total))
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_worker, (str(p), cache_dir)): str(p) for p in paths}
        for fut in as_completed(futs):
            if cancel and cancel():
                for f in futs:
                    f.cancel()
                break
            try:
                path, res, cached = fut.result()
            except Exception as e:
                path = futs[fut]
                res = {"ok": False, "text": "", "norm": "", "conf": 0.0,
                       "n_boxes": 0, "error": f"worker失败: {e}"}
                cached = False
            results[path] = res
            done += 1
            if progress:
                progress(done, total, Path(path).name, cached)
    return results


def warm_up():
    """预热 OCR（让模型加载进当前进程，便于测速）"""
    _get_ocr()
