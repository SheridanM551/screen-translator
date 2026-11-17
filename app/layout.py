"""Group OCR lines into text blocks (speech bubbles, dialogue boxes) and decide direction."""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

import numpy as np

from .ocr_oneocr import OcrLine

KANA = re.compile(r"[぀-ゟ゠-ヿｦ-ﾟ]")
CJK = re.compile(r"[㐀-鿿豈-﫿]")
JP_ANY = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]")


@dataclass
class _L:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float
    ori: str  # "V", "H" or "?" (single glyph)

    @property
    def w(self):
        return self.x1 - self.x0

    @property
    def h(self):
        return self.y1 - self.y0

    @property
    def size(self):  # glyph size: column width for V, line height for H
        if self.ori == "V":
            return self.w
        if self.ori == "H":
            return self.h
        return max(self.w, self.h)


@dataclass
class TextBlock:
    text: str
    rect: tuple[int, int, int, int]
    vertical: bool
    char_size: float
    conf: float
    lines: list = field(default_factory=list)

    @property
    def key(self) -> str:
        x0, y0, _, _ = self.rect
        return f"{self.text}@{x0 // 24},{y0 // 24}"


def _overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def _gap(a0, a1, b0, b1):
    return max(0.0, max(a0, b0) - min(a1, b1))


def _should_join(a: _L, b: _L) -> bool:
    sa, sb = a.size, b.size
    if max(sa, sb) > 1.7 * min(sa, sb):
        return False
    s = (sa + sb) / 2
    if a.ori == "V" and b.ori == "V":
        # neighbouring columns of the same bubble, or a column split into two pieces
        ov = _overlap(a.y0, a.y1, b.y0, b.y1)
        if ov >= 0.3 * min(a.h, b.h) and _gap(a.x0, a.x1, b.x0, b.x1) <= 0.9 * s:
            return True
        same_col = _overlap(a.x0, a.x1, b.x0, b.x1) >= 0.5 * min(a.w, b.w)
        return same_col and _gap(a.y0, a.y1, b.y0, b.y1) <= 0.8 * s
    if a.ori == "H" and b.ori == "H":
        ov = _overlap(a.x0, a.x1, b.x0, b.x1)
        # game dialogue leading is often 0.6–0.9 × glyph height
        if ov > 0.15 * min(a.w, b.w) and _gap(a.y0, a.y1, b.y0, b.y1) <= 1.0 * s:
            return True
        same_row = _overlap(a.y0, a.y1, b.y0, b.y1) >= 0.6 * min(a.h, b.h)
        return same_row and _gap(a.x0, a.x1, b.x0, b.x1) <= 0.8 * s
    # a single glyph (punctuation, short word) next to anything
    return (_gap(a.x0, a.x1, b.x0, b.x1) <= 0.6 * s and _gap(a.y0, a.y1, b.y0, b.y1) <= 0.6 * s)


def _join_text(group: list[_L], vertical: bool) -> str:
    if vertical:
        # columns right → left; pieces of one column top → bottom
        cols: list[list[_L]] = []
        for l in sorted(group, key=lambda l: -(l.x0 + l.x1) / 2):
            for c in cols:
                if _overlap(c[0].x0, c[0].x1, l.x0, l.x1) >= 0.5 * min(c[0].w, l.w):
                    c.append(l)
                    break
            else:
                cols.append([l])
        return "".join("".join(p.text for p in sorted(c, key=lambda l: l.y0)) for c in cols)
    rows: list[list[_L]] = []
    for l in sorted(group, key=lambda l: (l.y0 + l.y1) / 2):
        for r in rows:
            if _overlap(r[0].y0, r[0].y1, l.y0, l.y1) >= 0.5 * min(r[0].h, l.h):
                r.append(l)
                break
        else:
            rows.append([l])
    out = []
    for r in rows:
        parts = [p.text for p in sorted(r, key=lambda l: l.x0)]
        s = parts[0]
        for p in parts[1:]:
            s += (" " if s[-1:].isascii() and p[:1].isascii() else "") + p
        out.append(s)
    return "".join(out)


def group_lines(lines: list[OcrLine], min_conf: float = 0.5, require_kana: bool = True,
                vertical_ratio: float = 1.5) -> list[TextBlock]:
    items: list[_L] = []
    for ln in lines:
        t = ln.text.strip()
        if not t or ln.conf < min_conf:
            continue
        x0, y0, x1, y1 = ln.rect
        w, h = x1 - x0, y1 - y0
        if len(t) == 1:
            if not JP_ANY.search(t) and t not in "。、！？!?…ー":
                continue  # lone symbols are almost always art noise
            ori = "?"
        else:
            ori = "V" if h > vertical_ratio * w else "H"
        items.append(_L(t, x0, y0, x1, y1, ln.conf, ori))

    n = len(items)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if _should_join(items[i], items[j]):
                parent[find(i)] = find(j)

    groups: dict[int, list[_L]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(items[i])

    blocks: list[TextBlock] = []
    for g in groups.values():
        nv = sum(len(l.text) for l in g if l.ori == "V")
        nh = sum(len(l.text) for l in g if l.ori == "H")
        if nv == 0 and nh == 0:
            continue  # only stray single glyphs
        vertical = nv > nh
        text = _join_text(g, vertical)
        if require_kana and not KANA.search(text):
            continue
        if not require_kana and not JP_ANY.search(text):
            continue
        sizes = [l.size for l in g if l.ori != "?"]
        rect = (int(min(l.x0 for l in g)), int(min(l.y0 for l in g)),
                int(max(l.x1 for l in g) + 0.999), int(max(l.y1 for l in g) + 0.999))
        conf = sum(l.conf * len(l.text) for l in g) / max(1, sum(len(l.text) for l in g))
        blocks.append(TextBlock(text, rect, vertical, statistics.median(sizes), conf, g))
    return blocks


def reading_order(items: list, rect_of, vertical_of) -> list:
    """Manga (mostly vertical text): row bands top→bottom, right→left inside a band.
    Otherwise (games, horizontal text): top→bottom, left→right."""
    if not items:
        return []
    vert = sum(1 for i in items if vertical_of(i)) * 2 > len(items)
    rows: list[dict] = []
    for it in sorted(items, key=lambda i: rect_of(i)[1]):
        r = rect_of(it)
        for row in rows:
            y0, y1 = row["span"]
            if _overlap(y0, y1, r[1], r[3]) >= 0.3 * min(y1 - y0, r[3] - r[1]):
                row["items"].append(it)
                row["span"] = (min(y0, r[1]), max(y1, r[3]))
                break
        else:
            rows.append({"span": (r[1], r[3]), "items": [it]})
    rows.sort(key=lambda row: row["span"][0])
    out = []
    for row in rows:
        if vert:
            out += sorted(row["items"], key=lambda i: -(rect_of(i)[0] + rect_of(i)[2]))
        else:
            out += sorted(row["items"], key=lambda i: rect_of(i)[0])
    return out


def sample_background(frame: np.ndarray, rect, pad: int = 4) -> tuple[int, int, int]:
    """Median colour of a thin ring just outside rect. frame is BGRA (H, W, 4)."""
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = rect
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(w - 1, x1 + pad), min(h - 1, y1 + pad)
    ring = np.concatenate([
        frame[y0, x0:x1 + 1, :3], frame[y1, x0:x1 + 1, :3],
        frame[y0:y1 + 1, x0, :3], frame[y0:y1 + 1, x1, :3],
    ])
    b, g, r = np.median(ring, axis=0)
    return int(r), int(g), int(b)
