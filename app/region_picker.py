"""Drag-to-select screen region picker (PySide6, Windows).

    pick_region(cb, monitor_rect_physical=None, current_region=None)
        cb((x0, y0, x1, y1))  physical px, monitor-relative, x1/y1 exclusive
        cb("full")            user pressed Space (whole monitor)
        cb(None)              cancelled (Esc, right click, closed, or replaced by a new picker)

Runs inside the existing QApplication (no nested event loop, returns immediately).
May be called from any thread; the window and the callback always run on the GUI thread.
The callback fires ~60 ms after the dim window has been hidden, so a screen grab done
inside the callback no longer sees it. Only one picker exists at a time: a new call
cancels the old one (its callback receives None).

While idle the current region (``current_region``; None = whole monitor) is shown as red
corner brackets; a hint pill sits in the bottom-right corner.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import math
import sys
import threading
from typing import Callable, Optional, Union

from PySide6.QtCore import (QCoreApplication, QObject, QPointF, QRect, QRectF, Qt, QThread,
                            QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QPainter
from PySide6.QtWidgets import QWidget

from .region_highlight import paint_region_frame, region_label

__all__ = ["pick_region", "RegionPicker"]

Rect = tuple[int, int, int, int]
Result = Optional[Union[Rect, str]]   # rect | "full" | None

DIM = QColor(0, 0, 0, 110)
PILL = QColor(20, 20, 20, 225)
HINT_MAIN = "空白鍵：全螢幕"
HINT_SUB = "拖曳：選取範圍　Esc：取消"
REF_ARM_T = 0.12        # current-region brackets: how far the arms reach toward mid-edge
MIN_SIZE = 4            # physical px; smaller drags are treated as a click and ignored
CALLBACK_DELAY_MS = 60  # let DWM drop the dim window before the callback grabs the screen

_user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None
if _user32 is not None:
    _user32.GetForegroundWindow.restype = wt.HWND
    _user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wt.DWORD
    _user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    _user32.AttachThreadInput.restype = wt.BOOL
    _user32.SetForegroundWindow.argtypes = [wt.HWND]
    _user32.SetForegroundWindow.restype = wt.BOOL
    _user32.BringWindowToTop.argtypes = [wt.HWND]
    _user32.SetFocus.argtypes = [wt.HWND]
    _user32.SetFocus.restype = wt.HWND
    _user32.SetActiveWindow.argtypes = [wt.HWND]
    _user32.SetActiveWindow.restype = wt.HWND
    _kernel32.GetCurrentThreadId.restype = wt.DWORD


def _force_foreground(hwnd: int) -> bool:
    """Bring our window to the foreground even if another app currently owns it.
    Windows' foreground lock usually blocks SetForegroundWindow from a background process;
    attaching to the foreground thread's input queue is the standard, side-effect-free
    workaround (no fake key presses)."""
    if _user32 is None:
        return False
    fg = _user32.GetForegroundWindow()
    if fg == hwnd:
        return True
    if _user32.SetForegroundWindow(hwnd) and _user32.GetForegroundWindow() == hwnd:
        return True
    me = _kernel32.GetCurrentThreadId()
    other = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(other and other != me and _user32.AttachThreadInput(other, me, True))
    try:
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
        _user32.SetActiveWindow(hwnd)
        _user32.SetFocus(hwnd)
    finally:
        if attached:
            _user32.AttachThreadInput(other, me, False)
    return _user32.GetForegroundWindow() == hwnd


def _font(px: float, bold: bool) -> QFont:
    f = QFont()
    f.setFamilies(["Microsoft JhengHei UI", "Microsoft JhengHei", "Segoe UI"])
    f.setPixelSize(max(6, int(round(px))))
    f.setBold(bold)
    return f


class RegionPicker(QWidget):
    def __init__(self, callback: Callable[[Result], None],
                 monitor_rect_physical: tuple[int, int, int, int] | None = None,
                 current_region: Rect | None = None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setCursor(Qt.CrossCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setWindowTitle("選取翻譯範圍")
        self._cb = callback
        self._done = False
        self._p0: tuple[int, int] | None = None
        self._p1: tuple[int, int] | None = None
        self._dragging = False

        if monitor_rect_physical is None:
            scr = QGuiApplication.primaryScreen()
            g = scr.geometry()
            dpr = scr.devicePixelRatio()
            monitor_rect_physical = (g.x(), g.y(), round(g.width() * dpr), round(g.height() * dpr))
        left, top, w, h = (int(v) for v in monitor_rect_physical)
        self._mw, self._mh = w, h
        self._current: Rect | None = None
        if current_region is not None:
            x0, y0, x1, y1 = (int(round(v)) for v in current_region)
            x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
            if x1 > x0 and y1 > y0 and (x0, y0, x1, y1) != (0, 0, w, h):
                self._current = (x0, y0, x1, y1)
        scr = next((s for s in QGuiApplication.screens()
                    if s.geometry().x() == left and s.geometry().y() == top),
                   QGuiApplication.primaryScreen())
        dpr = scr.devicePixelRatio()
        self.setScreen(scr)
        self.setGeometry(QRect(left, top, math.ceil(w / dpr - 1e-6), math.ceil(h / dpr - 1e-6)))
        self._f_main = _font(17, True)
        self._f_sub = _font(12.5, False)
        self._f_size = _font(13, True)

    # ---- helpers ------------------------------------------------------------------
    def _phys(self, pos: QPointF) -> tuple[int, int]:
        d = self.devicePixelRatioF()
        x = min(max(int(math.floor(pos.x() * d + 1e-3)), 0), self._mw - 1)
        y = min(max(int(math.floor(pos.y() * d + 1e-3)), 0), self._mh - 1)
        return x, y

    def selection(self) -> Rect | None:
        """Current selection in physical px (x1/y1 exclusive), or None."""
        if self._p0 is None or self._p1 is None:
            return None
        (ax, ay), (bx, by) = self._p0, self._p1
        return min(ax, bx), min(ay, by), max(ax, bx) + 1, max(ay, by) + 1

    def _valid(self) -> bool:
        s = self.selection()
        return s is not None and s[2] - s[0] >= MIN_SIZE and s[3] - s[1] >= MIN_SIZE

    def _hint_rect(self) -> QRectF:
        """Bottom-right hint pill, logical px."""
        fm1, fm2 = QFontMetricsF(self._f_main), QFontMetricsF(self._f_sub)
        w = max(fm1.horizontalAdvance(HINT_MAIN), fm2.horizontalAdvance(HINT_SUB)) + 32
        h = fm1.height() + fm2.height() + 4 + 20
        return QRectF(self.width() - w - 24, self.height() - h - 24, w, h)

    def start(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        _force_foreground(int(self.winId()))
        self.setFocus(Qt.ActiveWindowFocusReason)
        # foreground may be refused while the system is busy: retry once a bit later
        QTimer.singleShot(150, lambda: (not self._done) and _force_foreground(int(self.winId())))

    def finish(self, result: Result) -> None:
        if self._done:
            return
        self._done = True
        cb = self._cb
        self.hide()
        self.close()
        _active.discard(self)
        QTimer.singleShot(CALLBACK_DELAY_MS, lambda: cb(result))

    # ---- events -------------------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._p0 = self._p1 = self._phys(e.position())
            self._dragging = True
            self.update()
        elif e.button() == Qt.RightButton:
            self.finish(None)

    def mouseMoveEvent(self, e):
        if self._dragging:
            self._p1 = self._phys(e.position())
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            self._p1 = self._phys(e.position())
            if self._valid():
                self.finish(self.selection())
            else:                                   # a click, not a drag: start over
                self._p0 = self._p1 = None
                self.update()

    def keyPressEvent(self, e):
        k = e.key()
        if k == Qt.Key_Escape:
            self.finish(None)
        elif k == Qt.Key_Space:
            self.finish("full")
        elif k in (Qt.Key_Return, Qt.Key_Enter):
            if self._valid():
                self.finish(self.selection())
        else:
            super().keyPressEvent(e)

    def closeEvent(self, e):
        if not self._done:            # closed externally (Alt+F4 etc.) -> cancel
            self._done = True
            cb = self._cb
            _active.discard(self)
            QTimer.singleShot(CALLBACK_DELAY_MS, lambda: cb(None))
        super().closeEvent(e)

    # ---- painting -----------------------------------------------------------------
    def paintEvent(self, e):
        p = QPainter(self)
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(e.rect(), DIM)
        dpr = self.devicePixelRatioF()
        tl = 1.0 / dpr
        sel = self.selection()
        mon = (self._mw, self._mh)
        if sel is not None:
            x0, y0, x1, y1 = sel
            p.save()
            p.scale(tl, tl)
            # nearly-transparent (alpha 1) instead of 0 so the hole still receives the mouse
            p.fillRect(QRectF(x0, y0, x1 - x0, y1 - y0), QColor(0, 0, 0, 1))
            p.restore()
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        if sel is None:
            # what the user is about to replace
            paint_region_frame(p, self._current, mon, dpr, t=REF_ARM_T,
                               label=region_label(self._current, mon, "目前範圍"))
        else:
            paint_region_frame(p, sel, mon, dpr, t=1.0, line_logical=2.0, glow=False)
            self._size_label(p, sel, tl)
        self._hint(p, sel, tl)
        p.end()

    def _size_label(self, p: QPainter, sel: Rect, tl: float) -> None:
        x0, y0, x1, y1 = sel
        text = f"{x1 - x0} × {y1 - y0}"
        fm = QFontMetricsF(self._f_size)
        w, h = fm.horizontalAdvance(text) + 14, fm.height() + 6
        lx = min(max(0.0, x0 * tl - 3), self.width() - w)
        ly = y0 * tl - h - 6
        if ly < 0:
            ly = y0 * tl + 6
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        p.setBrush(PILL)
        r = QRectF(lx, ly, w, h)
        p.drawRoundedRect(r, 4, 4)
        p.setFont(self._f_size)
        p.setPen(QColor(255, 255, 255))
        p.drawText(r, Qt.AlignCenter, text)

    def _hint(self, p: QPainter, sel: Rect | None, tl: float) -> None:
        r = self._hint_rect()
        p.save()
        if sel is not None:   # fade the hint if the selection runs underneath it
            x0, y0, x1, y1 = sel
            if QRectF(x0 * tl, y0 * tl, (x1 - x0) * tl, (y1 - y0) * tl).intersects(r):
                p.setOpacity(0.3)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setPen(QColor(255, 255, 255, 40))
        p.setBrush(PILL)
        p.drawRoundedRect(r, 9, 9)
        fm1 = QFontMetricsF(self._f_main)
        inner = r.adjusted(16, 10, -16, -10)
        p.setFont(self._f_main)
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRectF(inner.x(), inner.y(), inner.width(), fm1.height()),
                   Qt.AlignRight | Qt.AlignVCenter, HINT_MAIN)
        p.setFont(self._f_sub)
        p.setPen(QColor(215, 215, 215))
        p.drawText(QRectF(inner.x(), inner.y() + fm1.height() + 4, inner.width(),
                          inner.height() - fm1.height() - 4),
                   Qt.AlignRight | Qt.AlignVCenter, HINT_SUB)
        p.restore()


# ---------------------------------------------------------------------------- entry point
_active: set = set()


class _Invoker(QObject):
    call = Signal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._run, Qt.QueuedConnection)

    @staticmethod
    def _run(fn):
        fn()


_invoker: _Invoker | None = None
_inv_lock = threading.Lock()


def _gui_call(fn) -> None:
    global _invoker
    app = QCoreApplication.instance()
    if app is None:
        raise RuntimeError("pick_region needs a running QApplication")
    if QThread.currentThread() == app.thread():
        fn()
        return
    with _inv_lock:
        if _invoker is None:
            inv = _Invoker()
            inv.moveToThread(app.thread())
            _invoker = inv
    _invoker.call.emit(fn)


def pick_region(callback: Callable[[Result], None],
                monitor_rect_physical: tuple[int, int, int, int] | None = None,
                current_region: Rect | None = None) -> None:
    """Show the picker over the primary monitor (or ``monitor_rect_physical`` =
    (left, top, width, height) as mss reports it) and return immediately.

    ``callback`` gets a rect (x0, y0, x1, y1) in physical px relative to that monitor,
    the string ``"full"`` (Space = whole monitor), or ``None`` (cancel). It runs on the
    GUI thread. ``current_region`` (None = whole monitor) is drawn as a red reference frame.
    """
    def go():
        for old in list(_active):
            old.finish(None)
        w = RegionPicker(callback, monitor_rect_physical, current_region)
        _active.add(w)
        w.start()
    _gui_call(go)
