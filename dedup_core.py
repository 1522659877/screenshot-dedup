# -*- coding: utf-8 -*-
"""
dedup_core.py —— 去重核心
策略：
  1) 精确匹配：归一化文本完全一致 → 零误报，大部分重复在这里就被抓住
  2) 模糊匹配：字符 n-gram Dice 相似度（对 CJK 无分词依赖）
  3) 感知哈希：可选第二通道，兜住"文字识别不稳但画面几乎一样"的图
保留策略：默认留文件最大者（可在界面改）
删除策略：移入回收站，可恢复
"""
from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}


# ============================================================
# 数据结构
# ============================================================
@dataclass
class Item:
    path: Path
    norm: str = ""
    text: str = ""
    conf: float = 0.0
    n_boxes: int = 0
    error: str = ""
    phash: int = 0

    @property
    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    @property
    def size_h(self) -> str:
        return human_size(self.size)

    @property
    def is_text(self) -> bool:
        return bool(self.norm)


@dataclass
class Group:
    items: list = field(default_factory=list)
    keeper: Item = None
    match_type: str = ""
    min_sim: float = 1.0

    @property
    def to_delete(self) -> list:
        return [i for i in self.items if i is not self.keeper]

    @property
    def saved_bytes(self) -> int:
        return sum(i.size for i in self.to_delete)


@dataclass
class ScanResult:
    folder: str = ""
    recursive: bool = False
    total_images: int = 0
    items: list = field(default_factory=list)
    groups: list = field(default_factory=list)
    no_text: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    threshold: float = 0.85
    use_phash: bool = False
    elapsed: float = 0.0
    cancelled: bool = False

    @property
    def delete_count(self) -> int:
        return sum(len(g.to_delete) for g in self.groups)

    @property
    def delete_bytes(self) -> int:
        return sum(g.saved_bytes for g in self.groups)


# ============================================================
# 工具
# ============================================================
def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


# ============================================================
# 相似度：字符 n-gram Dice
# ============================================================
def _ngrams(s: str, n: int) -> Counter:
    if len(s) < n:
        return Counter()
    return Counter(s[i:i + n] for i in range(len(s) - n + 1))


def _dice(a: str, b: str, n: int) -> float:
    """多重集合 n-gram Dice 系数：2|A∩B| / (|A|+|B|)。考虑字频，不是集合。"""
    A, B = _ngrams(a, n), _ngrams(b, n)
    if not A and not B:
        return 1.0 if a == b else 0.0
    if not A or not B:
        return 0.0
    inter = sum((A & B).values())
    tot = sum(A.values()) + sum(B.values())
    return 2.0 * inter / tot if tot else 0.0


def text_similarity(a: str, b: str) -> float:
    """
    综合相似度 0~1，专为 OCR 文本设计：
      - 短文本（<4字）：直接比相等，n-gram 无意义
      - 长文本：2-gram / 3-gram Dice 加权，n 越大越严格
    实测：RapidOCR 下同一段文字的不同截图相似度 ≈ 0.99~1.00
    """
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    la, lb = len(a), len(b)
    if min(la, lb) < 4:
        return 1.0 if a == b else 0.0

    # 长度差过大直接判低（前提：长度差异比 OCR 误差大得多）
    if abs(la - lb) / max(la, lb) > 0.6:
        return 0.0

    return 0.55 * _dice(a, b, 3) + 0.45 * _dice(a, b, 2)


# ============================================================
# 感知哈希（可选第二通道）
# ============================================================
def compute_phash(path, hash_size: int = 8) -> int:
    """DCT 感知哈希，抗缩放/轻微调色。失败返回 0。"""
    try:
        import numpy as np
        from PIL import Image
        img = Image.open(path).convert("L").resize((32, 32), Image.LANCZOS)
        a = np.asarray(img, dtype=float)
        d = np.fft.fft2(a)
        low = np.abs(d[:hash_size, :hash_size]).flatten()
        low[0] = 0.0
        med = np.median(low[1:]) if len(low) > 1 else 0.0
        bits = (low > med).astype(np.uint8)
        out = 0
        for b in bits:
            out = (out << 1) | int(b)
        return out
    except Exception:
        return 0


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


# ============================================================
# 分组
# ============================================================
class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        p = self.p
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, x, y):
        a, b = self.find(x), self.find(y)
        if a != b:
            self.p[a] = b


def group_items(items, threshold=0.85, use_phash=False, phash_max_dist=8,
                progress=None, cancel=None):
    """
    返回 [Group, ...]（只含 >=2 张的组）
    progress(done, total, msg)
    """
    live = [it for it in items if it.is_text]
    n = len(live)
    if n < 2:
        return []

    uf = _UF(n)
    match_type = {}
    pair_sim = {}

    # ---------- 第 1 轮：精确匹配（哈希分桶，O(n)） ----------
    buckets: dict[str, list[int]] = {}
    for i, it in enumerate(live):
        buckets.setdefault(it.norm, []).append(i)
    exact_pairs = 0
    for norm, idxs in buckets.items():
        if len(idxs) > 1:
            for k in range(1, len(idxs)):
                uf.union(idxs[0], idxs[k])
                exact_pairs += 1

    # ---------- 第 2 轮：模糊匹配 ----------
    order = sorted(range(n), key=lambda i: len(live[i].norm))
    compared = 0
    fuzzy_pairs = 0
    total_est = max(1, n * (n - 1) // 2)

    for pos_i in range(n):
        if cancel and cancel():
            return []
        i = order[pos_i]
        ni = live[i].norm
        li = len(ni)
        if li == 0:
            continue
        for pos_j in range(pos_i + 1, n):
            j = order[pos_j]
            nj = live[j].norm
            lj = len(nj)
            # 已同组就不用再比
            if uf.find(i) == uf.find(j):
                continue
            # 长度差超过 35% 直接跳过（后续更长，break）
            if (lj - li) / max(li, lj) > 0.35:
                break
            compared += 1
            if abs(lj - li) / max(li, lj) > 0.6:
                continue
            s = text_similarity(ni, nj)
            if s >= threshold:
                uf.union(i, j)
                fuzzy_pairs += 1
                pair_sim[(i, j)] = s
        if progress and (pos_i % 10 == 0 or pos_i == n - 1):
            progress(pos_i + 1, n, f"相似度比对 {pos_i + 1}/{n}")

    # ---------- 第 3 轮：感知哈希（可选） ----------
    if use_phash:
        for pos_i in range(n):
            if cancel and cancel():
                return []
            i = order[pos_i]
            if not live[i].phash:
                continue
            for pos_j in range(pos_i + 1, n):
                j = order[pos_j]
                if uf.find(i) == uf.find(j) or not live[j].phash:
                    continue
                if hamming(live[i].phash, live[j].phash) <= phash_max_dist:
                    uf.union(i, j)

    # ---------- 收集 ----------
    raw: dict[int, list[int]] = {}
    for i in range(n):
        raw.setdefault(uf.find(i), []).append(i)

    groups = []
    for _, idxs in raw.items():
        if len(idxs) < 2:
            continue
        its = [live[i] for i in idxs]
        g = Group(items=its)
        # 判定匹配类型
        norms = {it.norm for it in its}
        if len(norms) == 1:
            g.match_type = "完全相同"
            g.min_sim = 1.0
        else:
            sims = []
            base = its[0].norm
            for it in its[1:]:
                sims.append(text_similarity(base, it.norm))
            g.min_sim = min(sims) if sims else 1.0
            if use_phash and g.min_sim < threshold:
                g.match_type = "画面相似"
            else:
                g.match_type = "内容相似"
        g.keeper = pick_keeper(its)
        groups.append(g)

    groups.sort(key=lambda g: g.saved_bytes, reverse=True)
    return groups


def pick_keeper(items) -> Item:
    """默认保留文件最大者（通常分辨率/质量最高）"""
    return max(items, key=lambda it: (it.size, len(it.norm), it.path.name))


# ============================================================
# 删除
# ============================================================
def delete_to_trash(path) -> tuple[bool, str]:
    try:
        from send2trash import send2trash
    except ImportError:
        return False, "未安装 send2trash，无法使用回收站"
    try:
        send2trash(str(path))
        return True, ""
    except Exception as e:
        return False, str(e)
