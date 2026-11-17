"""Red "current capture region" highlight (PySide6, Windows).

    hl = RegionHighlight((left, top, width, height))   # monitor rect as mss reports it
    hl.show_region((x0, y0, x1, y1))    # physical px, monitor-relative; None = whole monitor
    hl.hide_region()

Reveal animation (~300 ms): four short red corner brackets (the toolbar icon's style) grow
along the edges until they meet into a full rectangle while fading in; hide fades out
(~150 ms) and then hides the window. Calls can be repeated / interleaved freely (one timer,
state is just retargeted), so mouse jitter over the toolbar icon cannot glitch or leak.

The window is click-through, never takes focus, and is excluded from screen capture
(WDA_EXCLUDEFROMCAPTURE) so OCR never sees it. GUI-thread use only.

``paint_region_frame`` is the shared painter (also used by the region picker).
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import sys

from PySide6.QtCore import QElapsedTimer, QRect, QRectF, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication, QPainter,
                           QPainterPath)
from PySide6.QtWidgets import QWidget

__all__ = ["RegionHighlight", "paint_region_frame", "region_label"]

RED = QColor(255, 0, 0)
OUTLINE = QColor(0, 0, 0, 170)       # 1 px dark edge: keeps the frame visible on red/bright art
GLOW = QColor(255, 0, 0, 70)         # faint outer glow
LINE_LOGICAL = 3.0                   # frame thickness in logical px
START_ARM_LOGICAL = 22.0             # bracket arm length at the start of the reveal
SHOW_MS = 300
HIDE_MS = 150
FRAME_INTERVAL_MS = 15

Rect = tuple[int, int, int, int]


def region_label(rect: Rect | None, mon_size: tuple[int, int], prefix: str = "翻譯範圍") -> str:
    mw, mh = mon_size
    if rect is None or tuple(rect) == (0, 0, mw, mh):
        return f"{prefix}：全螢幕"
    x0, y0, x1, y1 = rect
    return f"{prefix} {x1 - x0}×{y1 - y0}"


def _ui_font(px: int, bold: bool = True) -> QFont:
    f = QFont()
    f.setFamilies(["Microsoft JhengHei UI", "Microsoft JhengHei", "Segoe UI"])
    f.setPixelSize(max(6, int(px)))
    f.setBold(bold)
    f.setStyleStrategy(QFont.PreferAntialias)
    return f


def paint_region_frame(p: QPainter, rect: Rect | None, mon_size: tuple[int, int], dpr: float,
                       t: float = 1.0, alpha: float = 1.0, label: str | None = None,
                       line_logical: float = LINE_LOGICAL, glow: bool = True,
                       start_arm_logical: float = START_ARM_LOGICAL) -> None:
    """Draw the red corner-bracket frame around ``rect`` (physical, monitor-relative;
    None = whole monitor).

    ``p`` paints in the monitor window's logical coordinates (origin = monitor top-left).
    ``t`` 0..1: 0 = short corner brackets, 1 = closed rectangle (arms meet mid-edge).
    The red band sits just OUTSIDE the rect (all region pixels stay visible) and is pushed
    inward wherever the rect touches the monitor edge, so a full-screen frame is inset.
    """
    mw, mh = mon_size
    x0, y0, x1, y1 = rect if rect is not None else (0, 0, mw, mh)
    if alpha <= 0 or x1 <= x0 or y1 <= y0:
        return
    lw = max(2, int(round(line_logical * dpr)))
    ox0, oy0 = max(0, x0 - lw), max(0, y0 - lw)
    ox1, oy1 = min(mw, x1 + lw), min(mh, y1 + lw)
    W, H = ox1 - ox0, oy1 - oy0
    if W < 2 * lw or H < 2 * lw:
        return
    t = min(max(t, 0.0), 1.0)
    a0 = start_arm_logical * dpr

    def arm(side: int) -> int:
        half = math.ceil(side / 2)
        s = min(half, a0)
        return int(round(s + (half - s) * t)) if t < 1 else half

    ah, av = max(arm(W), lw), max(arm(H), lw)
    # band rectangles (x, y, w, h) of the 8 arms, physical px
    bands = [
        (ox0, oy0, ah, lw), (ox0, oy0, lw, av),                     # top-left
        (ox1 - ah, oy0, ah, lw), (ox1 - lw, oy0, lw, av),           # top-right
        (ox0, oy1 - lw, ah, lw), (ox0, oy1 - av, lw, av),           # bottom-left
        (ox1 - ah, oy1 - lw, ah, lw), (ox1 - lw, oy1 - av, lw, av),  # bottom-right
    ]

    def layer(grow: int) -> QPainterPath:
        path = QPainterPath()
        path.setFillRule(Qt.WindingFill)        # overlaps are filled once (no double alpha)
        for bx, by, bw, bh in bands:
            path.addRect(QRectF(bx - grow, by - grow, bw + 2 * grow, bh + 2 * grow))
        return path

    p.save()
    p.scale(1.0 / dpr, 1.0 / dpr)               # physical px from here on
    p.setOpacity(p.opacity() * min(alpha, 1.0))
    p.setRenderHint(QPainter.Antialiasing, False)   # axis-aligned integer rects: crisp
    if glow:
        p.fillPath(layer(max(2, int(round(2 * dpr)))), GLOW)
    p.fillPath(layer(1), OUTLINE)
    p.fillPath(layer(0), RED)

    if label:
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        f = _ui_font(round(13 * dpr))
        fm = QFontMetricsF(f)
        padx, pady = 7 * dpr, 2 * dpr
        pw, ph = fm.horizontalAdvance(label) + 2 * padx, fm.height() + 2 * pady
        gap = 5 * dpr
        ix0, iy0 = ox0 + lw, oy0 + lw            # inside the band
        ix1, iy1 = ox1 - lw, oy1 - lw
        if ix1 - ix0 >= pw + 2 * gap and iy1 - iy0 >= ph + 2 * gap:
            lx, ly = ix0 + gap, iy0 + gap        # inside, top-left
        else:
            lx = min(max(0.0, ox0), mw - pw)
            ly = oy0 - ph - gap if oy0 - ph - gap >= 0 else min(oy1 + gap, mh - ph)
        r = QRectF(lx, ly, pw, ph)
        p.setPen(Qt.NoPen)
        p.setBrush(OUTLINE)
        p.drawRoundedRect(r.adjusted(-1, -1, 1, 1), 3 * dpr + 1, 3 * dpr + 1)
        p.setBrush(RED)
        p.drawRoundedRect(r, 3 * dpr, 3 * dpr)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255))
        p.drawText(r, Qt.AlignCenter, label)
    p.restore()


# ------------------------------------------------------------------------------- Win32
_user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None
GWL_EXSTYLE = -20
WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_LAYERED = 0x20, 0x80, 0x80000
WS_EX_NOACTIVATE, WS_EX_APPWINDOW = 0x08000000, 0x40000
WDA_NONE, WDA_EXCLUDEFROMCAPTURE = 0x0, 0x11
HWND_TOPMOST = wt.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
SWP_FRAMECHANGED, SWP_NOOWNERZORDER = 0x20, 0x200
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


def _ease_out(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return 1.0 - (1.0 - t) ** 3


class RegionHighlight(QWidget):
    """Click-through, capture-excluded red frame showing the current capture region."""

    def __init__(self, monitor_rect_physical: tuple[int, int, int, int], show_label: bool = True):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus
                         | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setWindowTitle("翻譯範圍")
        left, top, w, h = (int(v) for v in monitor_rect_physical)
        self._mw, self._mh = w, h
        scr = next((s for s in QGuiApplication.screens()
                    if s.geometry().x() == left and s.geometry().y() == top),
                   QGuiApplication.primaryScreen())
        dpr = scr.devicePixelRatio()
        self.setScreen(scr)
        self.setGeometry(QRect(left, top, math.ceil(w / dpr - 1e-6), math.ceil(h / dpr - 1e-6)))

        self.show_label = show_label
        self._rect: Rect = (0, 0, w, h)
        self._grow = 0.0          # linear 0..1 (eased when drawn)
        self._alpha = 0.0
        self._target = 0          # 1 = showing, 0 = hiding
        self._excluded = True
        self._hwnd = 0
        self._clock = QElapsedTimer()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(FRAME_INTERVAL_MS)
        self._timer.timeout.connect(self._step)
        self.winId()
        self._apply_native()

    # ---- public -------------------------------------------------------------------
    def show_region(self, rect_or_none: Rect | None) -> None:
        r = self._norm(rect_or_none)
        if not self.isVisible():
            self._rect, self._grow, self._alpha = r, 0.0, 0.0
            self._apply_native()                 # affinity in place before the 1st frame
            self.show()
            self._topmost()
        elif r != self._rect:                    # new region while visible: re-reveal it
            self._rect, self._grow = r, 0.0
        self._target = 1
        self._kick()

    def hide_region(self) -> None:
        self._target = 0
        if self.isVisible():
            self._kick()

    def is_showing(self) -> bool:
        return self.isVisible() and self._target == 1

    def set_capture_excluded(self, excluded: bool) -> None:
        """Debug/testing only."""
        self._excluded = bool(excluded)
        self._set_affinity()

    def display_affinity(self) -> int:
        v = wt.DWORD(0xFFFFFFFF)
        if _user32 and self._hwnd and _user32.GetWindowDisplayAffinity(self._hwnd, ctypes.byref(v)):
            return v.value
        return -1

    # ---- internals ----------------------------------------------------------------
    def _norm(self, rect) -> Rect:
        if rect is None:
            return 0, 0, self._mw, self._mh
        x0, y0, x1, y1 = (int(round(v)) for v in rect)
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self._mw, max(x1, x0 + 1)), min(self._mh, max(y1, y0 + 1))
        return x0, y0, x1, y1

    def _kick(self) -> None:
        if not self._timer.isActive():
            self._clock.start()
            self._timer.start()

    def _step(self) -> None:
        dt = min(self._clock.restart(), 100)     # ms; cap so a stall just jumps ahead
        if self._target:
            self._grow = min(1.0, self._grow + dt / SHOW_MS)
            self._alpha = min(1.0, self._alpha + dt / SHOW_MS)
            done = self._grow >= 1.0 and self._alpha >= 1.0
        else:
            self._alpha = max(0.0, self._alpha - dt / HIDE_MS)
            done = self._alpha <= 0.0
        self.update()
        if done:
            self._timer.stop()
            if not self._target:
                self.hide()
                self._grow = 0.0

    def paintEvent(self, ev) -> None:
        if self._alpha <= 0.0:
            return
        p = QPainter(self)
        label = region_label(self._rect, (self._mw, self._mh)) if self.show_label else None
        paint_region_frame(p, self._rect, (self._mw, self._mh), self.devicePixelRatioF(),
                           t=_ease_out(self._grow), alpha=_ease_out(self._alpha), label=label)
        p.end()

    def _render_state(self, grow: float, alpha: float) -> None:
        """Testing: freeze the animation at a given (linear) state."""
        self._timer.stop()
        self._grow, self._alpha = grow, alpha
        self.update()

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
        self._set_affinity()

    def _set_affinity(self) -> None:
        if _user32 is None or not self._hwnd:
            return
        ok = _user32.SetWindowDisplayAffinity(
            self._hwnd, WDA_EXCLUDEFROMCAPTURE if self._excluded else WDA_NONE)
        if not ok and self._excluded:
            print(f"[region_highlight] SetWindowDisplayAffinity failed "
                  f"({ctypes.get_last_error()}); the frame may show up in captures", file=sys.stderr)

    def _topmost(self) -> None:
        if _user32 is not None and self._hwnd:
            _user32.SetWindowPos(self._hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER)

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        if int(self.winId()) != self._hwnd:
            self._apply_native()
