"""Click-through, capture-excluded translation overlay (PySide6, Windows).

Coordinates handed to this module are PHYSICAL pixels relative to the top-left of the
target monitor (what ``mss`` reports).  Internally everything is laid out and rendered
in physical pixels; the painter is scaled by ``to_logical = 1 / devicePixelRatio`` so the
net device transform is the identity and patches land on exact screen pixels.

Startup order: create the QApplication BEFORE the first ``mss`` instance. Qt then makes the
process Per-Monitor-v2 DPI aware; mss' own SetProcessDpiAwareness call fails harmlessly.
(mss first -> process stays Per-Monitor v1; the overlay still lands correctly, but Qt windows
with frames lose PMv2 niceties.) Do not set QT_ENABLE_HIGHDPI_SCALING=0 or a rounding policy
unless you want to; the overlay reads the real devicePixelRatio either way.

Threading: ``Overlay.set_blocks / upsert_blocks / remove_blocks / clear / set_hidden /
set_capture_excluded`` may be called from any thread (they emit a queued signal).
``paint_block`` / ``layout_block`` need a live QGuiApplication (fonts) and may be used
from any thread for offscreen QImage rendering.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import os
import re
import sys
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication, QImage,
                           QPainter, QRegion)
from PySide6.QtWidgets import QWidget

__all__ = ["RenderBlock", "Overlay", "paint_block", "layout_block", "render_block_image",
           "BlockLayout", "auto_fg", "warmup"]


# --------------------------------------------------------------------------- data
@dataclass
class RenderBlock:
    id: str
    rect: tuple[int, int, int, int]   # x0, y0, x1, y1 physical px, monitor-relative (source bbox)
    text: str                          # translated text
    vertical: bool                     # lay the translation out vertically
    bg: tuple[int, int, int]           # patch fill colour
    fg: tuple[int, int, int] | None = None   # None -> black/white by bg luminance
    char_size: float = 0.0             # source glyph size (physical px); caps the font size; 0 = no cap


# --------------------------------------------------------------------------- tunables
FONT_FAMILIES = ["Microsoft JhengHei", "Microsoft JhengHei UI", "Microsoft YaHei"]
FONT_BOLD = True            # bold Hei reads better over art / at small sizes
PAD = 3                     # patch extends this many physical px beyond the source rect
TEXT_PAD = 1                # text area = source rect grown by this (=> 2 px margin in patch)
MIN_LOGICAL_PX = 8.0        # lower bound of the font size, in logical px
LINE_PITCH = 1.18           # horizontal: line advance / font px
COL_PITCH = 1.22            # vertical: column advance / font px
RADIUS_MAX = 6.0            # patch corner radius cap (physical px)
TOPMOST_INTERVAL_MS = 1000  # re-assert HWND_TOPMOST this often
_REF = 100                  # reference pixel size used for measuring (em = advance / _REF)
_EPS = 1e-6

# vertical presentation forms (Microsoft JhengHei has all of these)
_VERT_MAP = {
    "，": "︐", ",": "︐", "、": "︑", "。": "︒", "．": "︒", ".": "︒", "：": "︓", ":": "︓",
    "；": "︔", ";": "︔", "！": "︕", "!": "︕", "？": "︖", "?": "︖",
    "「": "﹁", "」": "﹂", "『": "﹃", "』": "﹄", "“": "﹁", "”": "﹂", "‘": "﹃", "’": "﹄",
    "（": "︵", "）": "︶", "(": "︵", ")": "︶", "【": "︻", "】": "︼", "《": "︽", "》": "︾",
    "〈": "︿", "〉": "﹀", "〔": "︹", "〕": "︺", "｛": "︷", "｝": "︸", "{": "︷", "}": "︸",
    "［": "﹇", "］": "﹈", "[": "﹇", "]": "﹈", "〖": "︗", "〗": "︘",
    "…": "︙", "⋯": "︙", "‥": "︰", "ー": "︱", "—": "︱", "―": "︱", "─": "︱", "－": "︱",
    "-": "︱", "–": "︲", "_": "︳", "＿": "︳",
}
# drawn rotated 90° clockwise in vertical text (no good presentation form)
_ROTATE = set("～~〜〰→←⇒")
# line-breaking rules: may not start a line / may not end a line
_NO_START = set("，。、．：；！？」』）】》〉〕｝］”’…‥⋯ー—―～〜,.:;!?)]}%·‧"
                "ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ々")
_NO_END = set("「『（【《〈〔｛［“‘([{")

_LATIN_H = re.compile(r"[A-Za-z0-9À-ɏ]+(?:['’.\-_/:+&][A-Za-z0-9À-ɏ]+)*%?")
_LATIN_V = re.compile(r"[A-Za-z0-9À-ɏ]+(?:[ '’.\-_/:+&][A-Za-z0-9À-ɏ]+)*%?")
_BANG = re.compile(r"[!?！？]+")
_HALF = {"！": "!", "？": "?"}


# --------------------------------------------------------------------------- fonts
_lock = threading.RLock()
_fonts: dict[int, QFont] = {}
_adv_cache: dict[str, float] = {}
_metrics: QFontMetricsF | None = None
_emc = 0.38   # visual centre of CJK glyphs above the baseline, in em


def _font(px: int) -> QFont:
    f = _fonts.get(px)
    if f is None:
        f = QFont()
        f.setFamilies(FONT_FAMILIES)
        f.setPixelSize(max(1, int(px)))
        f.setBold(FONT_BOLD)
        f.setStyleStrategy(QFont.StyleStrategy(QFont.PreferAntialias | QFont.PreferQuality))
        f.setHintingPreference(QFont.PreferVerticalHinting)  # keep advances linear in px
        f.setKerning(True)
        _fonts[px] = f
    return f


def _fm() -> QFontMetricsF:
    global _metrics, _emc
    if _metrics is None:
        _metrics = QFontMetricsF(_font(_REF))
        r = _metrics.tightBoundingRect("中")
        if r.height() > 0:
            _emc = -r.center().y() / _REF
    return _metrics


def warmup() -> None:
    """Load the font database / CJK font now (~1 s the first time) instead of on the first
    translated block. Called by Overlay.__init__; needs a QGuiApplication."""
    with _lock:
        _fm()
        _adv("中A")


def _adv(t: str) -> float:
    """Advance of ``t`` in em (font px = 1)."""
    a = _adv_cache.get(t)
    if a is None:
        a = _fm().horizontalAdvance(t) / _REF
        if len(_adv_cache) > 20000:
            _adv_cache.clear()
        _adv_cache[t] = a
    return a


def _rgb(c) -> tuple[int, int, int]:
    """Plain-int RGB tuple (accepts numpy scalars / arrays, RGBA)."""
    return int(c[0]), int(c[1]), int(c[2])


def _colors(block: "RenderBlock") -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    bg = _rgb(block.bg)
    return bg, (_rgb(block.fg) if block.fg is not None else auto_fg(bg))


def auto_fg(bg: tuple[int, int, int]) -> tuple[int, int, int]:
    """Black or white, whichever has the higher WCAG contrast against ``bg``."""
    def lin(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    lum = 0.2126 * lin(float(bg[0])) + 0.7152 * lin(float(bg[1])) + 0.0722 * lin(float(bg[2]))
    return (0, 0, 0) if (lum + 0.05) / 0.05 >= 1.05 / (lum + 0.05) else (255, 255, 255)


# --------------------------------------------------------------------------- tokenizer
# unit = (kind, text, adv_em)   kind: 'h' horizontal glyphs, 'u' upright cell,
#                                     't' tate-chu-yoko, 'r' rotated 90°, 's' space
class _Tok:
    __slots__ = ("units", "adv", "space", "src")

    def __init__(self, units, src, space=False):
        self.units = units
        self.adv = sum(u[2] for u in units)
        self.space = space
        self.src = src


def _is_mark(c: str) -> bool:
    return unicodedata.category(c) in ("Mn", "Me", "Cf") or "︀" <= c <= "️"


def _fullwidth(c: str) -> str:
    o = ord(c)
    return chr(o + 0xFEE0) if 0x21 <= o <= 0x7E else c


def _tokenize(text: str, vertical: bool) -> list[_Tok]:
    text = text.replace("\r", "").replace("\t", " ")
    toks: list[_Tok] = []
    i, n = 0, len(text)
    latin = _LATIN_V if vertical else _LATIN_H
    while i < n:
        c = text[i]
        if c == "\n":
            # explicit newlines are soft: the fitter decides the breaks. Between CJK they
            # vanish; between Latin words they become a space.
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < n else ""
            if not (prev.isascii() and prev.isalnum() and nxt.isascii() and nxt.isalnum()):
                i += 1
                continue
        if c == "\n" or c.isspace():
            adv = 1.0 if c == "　" else (0.5 if vertical else _adv(" "))
            toks.append(_Tok([("s", " ", adv)], c, space=True))
            i += 1
            continue
        if _is_mark(c) and toks and not toks[-1].space:   # combining / variation selector
            t = toks[-1]
            k, s, a = t.units[-1]
            t.units[-1] = (k, s + c, a)
            i += 1
            continue
        m = latin.match(text, i)
        if m:
            s = m.group()
            if not vertical:
                toks.append(_Tok([("h", ch, _adv(ch)) for ch in s], s))
            elif len(s) <= 2:
                toks.append(_Tok([("t", s, 1.0)], s))
            elif s.isdigit():
                toks.append(_Tok([("u", _fullwidth(ch), 1.0) for ch in s], s))
            else:
                toks.append(_Tok([("r", s, _adv(s))], s))
            i = m.end()
            continue
        if vertical:
            m = _BANG.match(text, i)
            if m and len(m.group()) == 2:
                s = "".join(_HALF.get(ch, ch) for ch in m.group())
                toks.append(_Tok([("t", s, 1.0)], m.group()))
                i = m.end()
                continue
            if c in _ROTATE:
                toks.append(_Tok([("r", c, max(_adv(c), 0.5))], c))
            else:
                v = _VERT_MAP.get(c) or _fullwidth(c)
                toks.append(_Tok([("u", v, 1.0)], c))
        else:
            toks.append(_Tok([("h", c, _adv(c))], c))
        i += 1

    # kinsoku: glue closing punctuation to the previous token, opening to the next
    out: list[_Tok] = []
    for t in toks:
        if (out and not t.space and not out[-1].space
                and (t.src[0] in _NO_START or out[-1].src[-1] in _NO_END)
                and len(out[-1].units) < 12):
            p = out[-1]
            out[-1] = _Tok(p.units + t.units, p.src + t.src)
        else:
            out.append(t)
    while out and out[-1].space:
        out.pop()
    while out and out[0].space:
        out.pop(0)
    return out


def _explode(u):
    """Split an over-long multi-glyph unit into per-character units."""
    k, s, _ = u
    if k in ("r", "h", "t") and len(s) > 1:
        kk = "r" if k == "t" else k
        return [(kk, ch, _adv(ch)) for ch in s]
    return [u]


def _wrap(toks: list[_Tok], cap: float) -> tuple[list[tuple[list, float]], bool]:
    """Greedy line breaking. Returns ([(units, length_em)], all_units_fit)."""
    lines: list[tuple[list, float]] = []
    cur: list = []
    cur_len = 0.0
    ok = True

    def flush():
        nonlocal cur, cur_len
        while cur and cur[-1][0] == "s":
            cur_len -= cur.pop()[2]
        if cur:
            lines.append((cur, cur_len))
        cur, cur_len = [], 0.0

    lim = cap + _EPS
    for t in toks:
        if t.space:
            if cur:
                cur.extend(t.units)
                cur_len += t.adv
            continue
        if cur_len + t.adv <= lim:
            cur.extend(t.units)
            cur_len += t.adv
            continue
        flush()
        if t.adv <= lim:
            cur, cur_len = list(t.units), t.adv
            continue
        for u0 in t.units:           # token longer than a line: break inside it
            for u in (_explode(u0) if u0[2] > lim else (u0,)):
                if cur and cur_len + u[2] > lim:
                    flush()
                cur.append(u)
                cur_len += u[2]
                if u[2] > lim:
                    ok = False
    flush()
    return lines, ok


# --------------------------------------------------------------------------- layout
class BlockLayout:
    """Position-independent layout of one block (all values physical px).

    The patch's top-left is at ``rect.x0 + dx, rect.y0 + dy``; ``ops`` are relative to it.
    """
    __slots__ = ("pw", "ph", "dx", "dy", "px", "ops", "fitted", "radius")

    def __init__(self, pw, ph, dx, dy, px, ops, fitted):
        self.pw, self.ph, self.dx, self.dy = pw, ph, dx, dy
        self.px, self.ops, self.fitted = px, ops, fitted
        self.radius = min(RADIUS_MAX, 0.25 * min(pw, ph))


_layout_cache: OrderedDict = OrderedDict()
_LAYOUT_CACHE_MAX = 2048


def _min_px(to_logical: float) -> int:
    return max(1, math.ceil(MIN_LOGICAL_PX / max(to_logical, 1e-3) - 1e-6))


def _rect_ints(rect) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = (int(round(v)) for v in rect)
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def layout_block(block: RenderBlock, to_logical: float = 1.0) -> BlockLayout:
    """Fit ``block.text`` into its rect (cached by content+size, not by position)."""
    x0, y0, x1, y1 = _rect_ints(block.rect)
    key = (block.text, x1 - x0, y1 - y0, bool(block.vertical),
           round(float(block.char_size or 0.0), 1), _min_px(to_logical))
    with _lock:
        lay = _layout_cache.get(key)
        if lay is not None:
            _layout_cache.move_to_end(key)
            return lay
        lay = _compute_layout(*key)
        _layout_cache[key] = lay
        if len(_layout_cache) > _LAYOUT_CACHE_MAX:
            _layout_cache.popitem(last=False)
        return lay


def _compute_layout(text: str, rw: int, rh: int, vertical: bool, char_size: float,
                    lo: int) -> BlockLayout:
    _fm()
    aw, ah = rw + 2 * TEXT_PAD, rh + 2 * TEXT_PAD           # text area
    off = PAD - TEXT_PAD                                     # text area inset in patch
    pw, ph = rw + 2 * PAD, rh + 2 * PAD
    toks = _tokenize(text, vertical)
    if not toks:
        return BlockLayout(pw, ph, -PAD, -PAD, lo, [], True)

    flow, cross = (ah, aw) if vertical else (aw, ah)
    pitch = COL_PITCH if vertical else LINE_PITCH

    def attempt(s: int):
        lines, ok = _wrap(toks, flow / s)
        need = s * (1 + (len(lines) - 1) * pitch)
        return (ok and need <= cross + 0.01), lines

    hi = int(min(char_size if char_size > 0 else 1e9, flow, cross))
    best = None
    if hi >= lo:
        ok, lines = attempt(lo)
        if ok:
            a, b = lo, hi            # invariant: attempt(a) fits
            best = (lo, lines)
            while a < b:
                mid = (a + b + 1) // 2
                ok, lines = attempt(mid)
                if ok:
                    a, best = mid, (mid, lines)
                else:
                    b = mid - 1
    fitted = best is not None
    dx, dy = -PAD, -PAD
    if fitted:
        s, lines = best
    else:
        # does not fit even at the minimum size: keep the minimum size, grow the patch
        # away from the reading start (horizontal: downward, vertical: leftward) and,
        # if a single glyph run is longer than the line, along the line direction too.
        s = lo
        max_unit = max((u[2] for t in toks for u in t.units), default=1.0)
        cap = max(flow / s, max_unit)
        lines, _ = _wrap(toks, cap)
        need_flow = max(flow, int(math.ceil(max(ln for _, ln in lines) * s)))
        need_cross = max(cross, int(math.ceil(s * (1 + (len(lines) - 1) * pitch))))
        g_flow, g_cross = need_flow - flow, need_cross - cross
        if vertical:
            aw, ah = need_cross, need_flow
            dx -= g_cross                 # grow leftward
            dy -= g_flow // 2             # (rare) grow vertically around the centre
        else:
            aw, ah = need_flow, need_cross
            dx -= g_flow // 2             # (rare) grow horizontally around the centre
        flow, cross = (ah, aw) if vertical else (aw, ah)
        pw, ph = aw + 2 * off, ah + 2 * off

    # balance: shortest line length that keeps the same number of lines
    n = len(lines)
    if n > 1:
        lo_c = max(sum(ln for _, ln in lines) / n, max(u[2] for t in toks for u in t.units))
        hi_c = flow / s
        for _ in range(12):
            mid = (lo_c + hi_c) / 2
            l2, ok2 = _wrap(toks, mid)
            if ok2 and len(l2) <= n:
                hi_c = mid
            else:
                lo_c = mid
        l2, ok2 = _wrap(toks, hi_c)
        if ok2 and len(l2) <= n:
            lines = l2
            n = len(lines)

    ops = []
    emc = _emc
    if not vertical:
        fm = QFontMetricsF(_font(s))
        content_h = s * (1 + (n - 1) * pitch)
        top = off + (ah - content_h) / 2
        for i, (units, _) in enumerate(lines):
            t = "".join(u[1] for u in units).strip()
            w = fm.horizontalAdvance(t)
            sx = 1.0
            if w > aw:                    # hinting/kerning drift: squeeze, never clip
                sx = aw / w
            x = off + (aw - w * sx) / 2
            base = top + s / 2 + i * pitch * s + emc * s
            ops.append(("h", x, base, t, sx, w))
    else:
        fm = QFontMetricsF(_font(s))
        group_w = s * (1 + (n - 1) * pitch)
        right = off + (aw + group_w) / 2
        col_h = max(ln for _, ln in lines) * s
        top = off + (ah - col_h) / 2
        for i, (units, _) in enumerate(lines):
            cx = right - s / 2 - i * pitch * s
            y = top
            for k, t, a in units:
                h = a * s
                if k == "u":
                    w = fm.horizontalAdvance(t)
                    ops.append(("h", cx - w / 2, y + h / 2 + emc * s, t, 1.0, w))
                elif k == "t":
                    w = fm.horizontalAdvance(t)
                    sx = min(1.0, 1.0 * s / w) if w > 0 else 1.0
                    ops.append(("t", cx, y + h / 2 + emc * s, t, sx, w))
                elif k == "r":
                    w = fm.horizontalAdvance(t)
                    ops.append(("r", cx, y + (h - w) / 2, t, 1.0, w))
                y += h
    return BlockLayout(pw, ph, dx, dy, s, ops, fitted)


# --------------------------------------------------------------------------- painting
def _patch_pos(block: RenderBlock, lay: BlockLayout,
               bounds: tuple[int, int, int, int] | None) -> tuple[int, int]:
    x0, y0, _, _ = _rect_ints(block.rect)
    x, y = x0 + lay.dx, y0 + lay.dy
    if bounds is not None:
        bx, by, bw, bh = bounds
        x = bx if lay.pw >= bw else min(max(x, bx), bx + bw - lay.pw)
        y = by if lay.ph >= bh else min(max(y, by), by + bh - lay.ph)
    return x, y


def _draw_layout(p: QPainter, lay: BlockLayout, x: float, y: float,
                 bg: tuple[int, int, int], fg: tuple[int, int, int]) -> None:
    p.save()
    p.translate(x, y)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setRenderHint(QPainter.TextAntialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(*bg))
    r = lay.radius
    p.drawRoundedRect(QRectF(0, 0, lay.pw, lay.ph), r, r)
    if lay.ops:
        p.setFont(_font(lay.px))
        p.setPen(QColor(*fg))
        drawText = p.drawText
        for kind, ox, oy, t, sx, w in lay.ops:
            if kind == "h":
                if sx == 1.0:
                    drawText(QPointF(ox, oy), t)
                else:
                    p.save(); p.translate(ox, oy); p.scale(sx, 1.0)
                    drawText(QPointF(0, 0), t)
                    p.restore()
            elif kind == "t":
                p.save(); p.translate(ox, oy); p.scale(sx, 1.0)
                drawText(QPointF(-w / 2, 0), t)
                p.restore()
            else:  # 'r': rotated 90° clockwise, (ox, oy) = column centre / run start
                p.save(); p.translate(ox, oy); p.rotate(90)
                drawText(QPointF(0, _emc * lay.px), t)
                p.restore()
    p.restore()


def paint_block(painter: QPainter, block: RenderBlock, to_logical: float = 1.0,
                bounds: tuple[int, int, int, int] | None = None) -> None:
    """Paint one block with vector drawing.

    ``painter`` coordinates are expected to be logical, with the monitor's top-left at
    (0, 0); ``to_logical`` = 1/devicePixelRatio (use 1.0 for a physical-size QImage).
    ``bounds`` (physical x, y, w, h) keeps a grown patch on screen.
    """
    lay = layout_block(block, to_logical)
    x, y = _patch_pos(block, lay, bounds)
    painter.save()
    painter.scale(to_logical, to_logical)
    _draw_layout(painter, lay, x, y, *_colors(block))
    painter.restore()


def render_block_image(block: RenderBlock, to_logical: float = 1.0,
                       bounds: tuple[int, int, int, int] | None = None
                       ) -> tuple[QImage, int, int]:
    """Rasterise one block's patch at physical resolution -> (image, x, y)."""
    lay = layout_block(block, to_logical)
    x, y = _patch_pos(block, lay, bounds)
    img = QImage(lay.pw, lay.ph, QImage.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    _draw_layout(p, lay, 0, 0, *_colors(block))
    p.end()
    return img, x, y


# --------------------------------------------------------------------------- Win32
_user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_APPWINDOW = 0x00040000
WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11
HWND_TOPMOST = wt.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x0001, 0x0002, 0x0004
SWP_NOACTIVATE, SWP_FRAMECHANGED, SWP_NOOWNERZORDER = 0x0010, 0x0020, 0x0200
SWP_NOSENDCHANGING = 0x0400

if _user32 is not None:
    _user32.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
    _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.SetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
    _user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    _user32.SetWindowPos.restype = wt.BOOL
    _user32.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]
    _user32.SetWindowDisplayAffinity.restype = wt.BOOL
    _user32.GetWindowDisplayAffinity.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _user32.GetWindowDisplayAffinity.restype = wt.BOOL
    _user32.GetForegroundWindow.restype = wt.HWND
    _user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wt.DWORD
    _user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    _user32.GetWindowRect.restype = wt.BOOL


# --------------------------------------------------------------------------- widget
class _Entry:
    __slots__ = ("key", "img", "x", "y")

    def __init__(self, key, img, x, y):
        self.key, self.img, self.x, self.y = key, img, x, y


class Overlay(QWidget):
    """Full-monitor, click-through, capture-excluded overlay that paints translation patches.

    Shown immediately on construction (pass ``show=False`` to defer).
    """
    _cmd = Signal(object)

    def __init__(self, monitor_rect_physical: tuple[int, int, int, int], show: bool = True):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus
                         | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setWindowTitle("translation overlay")

        left, top, width, height = (int(v) for v in monitor_rect_physical)
        self._mon = (left, top, width, height)
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self._img_cache: OrderedDict = OrderedDict()
        self._hidden = False
        self._excluded = True
        self._hwnd = 0
        self.last_paint_ms = 0.0
        self.affinity_ok = False

        # place on the matching QScreen (Qt6 keeps each screen's native origin; only the
        # size is divided by the screen's devicePixelRatio)
        scr = None
        for s in QGuiApplication.screens():
            g = s.geometry()
            if g.x() == left and g.y() == top:
                scr = s
                break
        scr = scr or QGuiApplication.primaryScreen()
        dpr = scr.devicePixelRatio()
        self.setScreen(scr)
        self.setGeometry(QRect(left, top, math.ceil(width / dpr - 1e-6),
                               math.ceil(height / dpr - 1e-6)))

        self._cmd.connect(self._on_cmd, Qt.QueuedConnection)
        self._timer = QTimer(self)
        self._timer.setInterval(TOPMOST_INTERVAL_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        warmup()
        self.winId()            # create the HWND now so the affinity is set before 1st frame
        self._apply_native()
        if show:
            self.show()

    # ---- public, thread-safe API --------------------------------------------------
    def set_blocks(self, blocks: list[RenderBlock]) -> None:
        self._cmd.emit(("set", list(blocks)))

    def upsert_blocks(self, blocks: list[RenderBlock]) -> None:
        self._cmd.emit(("upsert", list(blocks)))

    def remove_blocks(self, ids: list[str]) -> None:
        self._cmd.emit(("remove", list(ids)))

    def clear(self) -> None:
        self._cmd.emit(("clear", None))

    def set_hidden(self, hidden: bool) -> None:
        self._cmd.emit(("hidden", bool(hidden)))

    def set_capture_excluded(self, excluded: bool) -> None:
        """Debug/testing only: turn WDA_EXCLUDEFROMCAPTURE off/on."""
        self._cmd.emit(("exclude", bool(excluded)))

    # ---- info ---------------------------------------------------------------------
    @property
    def hwnd(self) -> int:
        return self._hwnd

    def display_affinity(self) -> int:
        v = wt.DWORD(0xFFFFFFFF)
        if _user32 and self._hwnd and _user32.GetWindowDisplayAffinity(self._hwnd, ctypes.byref(v)):
            return v.value
        return -1

    def native_rect(self) -> tuple[int, int, int, int]:
        r = wt.RECT()
        if _user32 and self._hwnd:
            _user32.GetWindowRect(self._hwnd, ctypes.byref(r))
        return r.left, r.top, r.right - r.left, r.bottom - r.top

    def to_logical(self) -> float:
        return 1.0 / self.devicePixelRatioF()

    # ---- native window setup ------------------------------------------------------
    def _apply_native(self) -> None:
        if _user32 is None:
            return
        hwnd = int(self.winId())
        self._hwnd = hwnd
        ex = _user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        want = (ex | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE
                | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        if want != ex:
            _user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, want)
            _user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER
                                 | SWP_NOACTIVATE | SWP_FRAMECHANGED)
        self._set_affinity(self._excluded)

    def _set_affinity(self, excluded: bool) -> None:
        if _user32 is None or not self._hwnd:
            return
        ok = _user32.SetWindowDisplayAffinity(self._hwnd,
                                              WDA_EXCLUDEFROMCAPTURE if excluded else WDA_NONE)
        if excluded:
            self.affinity_ok = bool(ok)
            if not ok:
                print(f"[overlay] SetWindowDisplayAffinity failed (err {ctypes.get_last_error()});"
                      " the overlay WILL be visible to screen capture", file=sys.stderr)

    def _tick(self) -> None:
        if _user32 is None or not self.isVisible():
            return
        if int(self.winId()) != self._hwnd:          # HWND recreated -> re-apply everything
            self._apply_native()
        fg = _user32.GetForegroundWindow()
        if fg:
            pid = wt.DWORD()
            _user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            if pid.value == os.getpid():               # don't cover our own UI (picker etc.)
                return
        _user32.SetWindowPos(self._hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                             SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER
                             | SWP_NOSENDCHANGING)

    def showEvent(self, ev):
        super().showEvent(ev)
        self._apply_native()

    # ---- GUI-thread command handling ----------------------------------------------
    def _bounds(self) -> tuple[int, int, int, int]:
        return 0, 0, self._mon[2], self._mon[3]

    def _phys_to_logical_rect(self, x: int, y: int, w: int, h: int) -> QRect:
        tl = self.to_logical()
        return QRect(math.floor(x * tl) - 1, math.floor(y * tl) - 1,
                     math.ceil(w * tl) + 3, math.ceil(h * tl) + 3)

    def _dirty(self, region: QRegion, e: _Entry) -> QRegion:
        return region.united(self._phys_to_logical_rect(e.x, e.y, e.img.width(), e.img.height()))

    def _upsert(self, b: RenderBlock, region: QRegion) -> QRegion:
        tl = self.to_logical()
        bg, fg = _colors(b)
        lay = layout_block(b, tl)
        x, y = _patch_pos(b, lay, self._bounds())
        x0, y0, x1, y1 = _rect_ints(b.rect)
        key = (b.text, x1 - x0, y1 - y0, bool(b.vertical), round(float(b.char_size or 0), 1),
               _min_px(tl), bg, fg)
        old = self._entries.get(b.id)
        if old is not None and old.key == key and old.x == x and old.y == y:
            return region
        img = self._img_cache.get(key)
        if img is None:
            img = QImage(lay.pw, lay.ph, QImage.Format_ARGB32_Premultiplied)
            img.fill(0)
            p = QPainter(img)
            _draw_layout(p, lay, 0, 0, bg, fg)
            p.end()
            self._img_cache[key] = img
            if len(self._img_cache) > 256:
                self._img_cache.popitem(last=False)
        else:
            self._img_cache.move_to_end(key)
        if old is not None:
            region = self._dirty(region, old)
        e = _Entry(key, img, x, y)
        self._entries[b.id] = e
        return self._dirty(region, e)

    def _on_cmd(self, cmd) -> None:
        op, arg = cmd
        region = QRegion()
        if op == "set":
            keep = {b.id for b in arg}
            for bid in [k for k in self._entries if k not in keep]:
                region = self._dirty(region, self._entries.pop(bid))
            for b in arg:
                region = self._upsert(b, region)
        elif op == "upsert":
            for b in arg:
                region = self._upsert(b, region)
        elif op == "remove":
            for bid in arg:
                e = self._entries.pop(bid, None)
                if e is not None:
                    region = self._dirty(region, e)
        elif op == "clear":
            for e in self._entries.values():
                region = self._dirty(region, e)
            self._entries.clear()
        elif op == "hidden":
            if arg != self._hidden:
                self._hidden = arg
                self.update()
            return
        elif op == "exclude":
            self._excluded = arg
            self._set_affinity(arg)
            return
        if not self._hidden and not region.isEmpty():
            self.update(region)

    def paintEvent(self, ev) -> None:
        if self._hidden or not self._entries:
            return
        t0 = time.perf_counter()
        p = QPainter(self)
        tl = self.to_logical()
        p.scale(tl, tl)                       # draw in physical px -> net transform = identity
        r = ev.rect()
        inv = 1.0 / tl
        rx0, ry0 = r.x() * inv - 2, r.y() * inv - 2
        rx1, ry1 = (r.x() + r.width()) * inv + 2, (r.y() + r.height()) * inv + 2
        for e in self._entries.values():
            img = e.img
            if e.x < rx1 and e.y < ry1 and e.x + img.width() > rx0 and e.y + img.height() > ry0:
                p.drawImage(e.x, e.y, img)
        p.end()
        self.last_paint_ms = (time.perf_counter() - t0) * 1000.0
