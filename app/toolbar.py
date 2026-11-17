"""Auto-hiding floating toolbar (top-centre of the screen, icon-only).

Collapsed it is a tiny click-through handle; dwelling the cursor at the top edge above it
slides the bar down, leaving it collapses it again. Excluded from screen capture.
"""
from __future__ import annotations

import ctypes
import math
from typing import Callable

from PySide6.QtCore import (QEasingCurve, QPoint, QPointF, QPropertyAnimation, QRect, QRectF,
                            QSize, Qt, QTimer, Signal)
from PySide6.QtGui import (QAction, QActionGroup, QColor, QCursor, QGuiApplication, QPainter,
                           QPainterPath, QPen)
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QLayout, QMenu, QToolTip, QWidget

FG = QColor(232, 232, 236)
RED = QColor(255, 59, 48)
BAR_BG = QColor(28, 28, 32, 228)
HANDLE_BG = QColor(150, 150, 160, 150)
STATUS = {"run": QColor(48, 209, 88), "load": QColor(255, 159, 10), "pause": QColor(142, 142, 147),
          "error": QColor(255, 69, 58)}
BTN = 34          # logical px
HANDLE = QSize(56, 6)
DWELL_MS = 120    # cursor must rest at the top edge this long before the bar opens
LEAVE_MS = 650    # ...and be away this long before it closes

_GWL_EXSTYLE = -20
_WS_EX_TRANSPARENT, _WS_EX_LAYERED, _WS_EX_NOACTIVATE, _WS_EX_TOOLWINDOW = 0x20, 0x80000, 0x08000000, 0x80
try:
    _u32 = ctypes.WinDLL("user32", use_last_error=True)
    _u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    _u32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _u32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    _u32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
except Exception:  # pragma: no cover
    _u32 = None


def _set_exstyle(w: QWidget, add: int = 0, remove: int = 0):
    if _u32 is None:
        return
    h = ctypes.c_void_p(int(w.winId()))
    s = _u32.GetWindowLongPtrW(h, _GWL_EXSTYLE)
    _u32.SetWindowLongPtrW(h, _GWL_EXSTYLE, (s | add) & ~remove)


def _exclude_from_capture(w: QWidget):
    try:
        ctypes.windll.user32.SetWindowDisplayAffinity(int(w.winId()), 0x11)
    except Exception:
        pass


# ---------------------------------------------------------------- icons
def _pen(c: QColor, w: float) -> QPen:
    p = QPen(c, w)
    p.setCapStyle(Qt.RoundCap)
    p.setJoinStyle(Qt.RoundJoin)
    return p


def icon_play(p: QPainter, r: QRectF, c: QColor):
    s = r.width() * 0.34
    cx, cy = r.center().x() + s * 0.12, r.center().y()
    path = QPainterPath(QPointF(cx - s * 0.55, cy - s * 0.7))
    path.lineTo(cx + s * 0.75, cy)
    path.lineTo(cx - s * 0.55, cy + s * 0.7)
    path.closeSubpath()
    p.setPen(_pen(c, 1.6))
    p.setBrush(c)
    p.drawPath(path)


def icon_pause(p: QPainter, r: QRectF, c: QColor):
    s = r.width() * 0.34
    cx, cy = r.center().x(), r.center().y()
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    for dx in (-s * 0.42, s * 0.42):
        p.drawRoundedRect(QRectF(cx + dx - s * 0.2, cy - s * 0.65, s * 0.4, s * 1.3), 1.5, 1.5)


def icon_eye(p: QPainter, r: QRectF, c: QColor, off: bool = False):
    cx, cy, w = r.center().x(), r.center().y(), r.width() * 0.62
    path = QPainterPath(QPointF(cx - w / 2, cy))
    path.quadTo(cx, cy - w * 0.62, cx + w / 2, cy)
    path.quadTo(cx, cy + w * 0.62, cx - w / 2, cy)
    p.setPen(_pen(c, 1.8))
    p.setBrush(Qt.NoBrush)
    p.drawPath(path)
    p.setBrush(c)
    p.drawEllipse(QPointF(cx, cy), w * 0.13, w * 0.13)
    if off:
        p.setPen(_pen(BAR_BG, 4.5))
        p.drawLine(QPointF(cx - w * 0.42, cy + w * 0.42), QPointF(cx + w * 0.42, cy - w * 0.42))
        p.setPen(_pen(c, 1.8))
        p.drawLine(QPointF(cx - w * 0.42, cy + w * 0.42), QPointF(cx + w * 0.42, cy - w * 0.42))


def icon_region(p: QPainter, r: QRectF, c: QColor = RED):
    """Square whose four sides are broken in the middle (corner brackets) + centre crosshair."""
    s = r.width() * 0.56
    x0, y0 = r.center().x() - s / 2, r.center().y() - s / 2
    x1, y1 = x0 + s, y0 + s
    a = s * 0.3
    p.setPen(_pen(c, 2.0))
    p.setBrush(Qt.NoBrush)
    for (x, y, dx, dy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
        p.drawLine(QPointF(x, y), QPointF(x + dx * a, y))
        p.drawLine(QPointF(x, y), QPointF(x, y + dy * a))
    cx, cy, k, g = r.center().x(), r.center().y(), s * 0.2, s * 0.06
    p.setPen(_pen(c, 1.6))
    p.drawLine(QPointF(cx - k, cy), QPointF(cx - g, cy))
    p.drawLine(QPointF(cx + g, cy), QPointF(cx + k, cy))
    p.drawLine(QPointF(cx, cy - k), QPointF(cx, cy - g))
    p.drawLine(QPointF(cx, cy + g), QPointF(cx, cy + k))


def icon_reset(p: QPainter, r: QRectF, c: QColor):
    rad = r.width() * 0.22
    cx, cy = r.center().x(), r.center().y()
    box = QRectF(cx - rad, cy - rad, 2 * rad, 2 * rad)
    p.setPen(_pen(c, 1.8))
    p.setBrush(Qt.NoBrush)
    p.drawArc(box, 60 * 16, 280 * 16)
    # arrow head at the arc start (angle 60°)
    ang = math.radians(60)
    tip = QPointF(cx + rad * math.cos(ang), cy - rad * math.sin(ang))
    h = rad * 0.55
    p.setBrush(c)
    head = QPainterPath(tip + QPointF(h * 0.9, -h * 0.1))
    head.lineTo(tip + QPointF(-h * 0.35, -h * 0.75))
    head.lineTo(tip + QPointF(-h * 0.1, h * 0.6))
    head.closeSubpath()
    p.setPen(Qt.NoPen)
    p.drawPath(head)


def icon_gear(p: QPainter, r: QRectF, c: QColor):
    cx, cy = r.center().x(), r.center().y()
    ro, ri, n = r.width() * 0.27, r.width() * 0.2, 8
    path = QPainterPath()
    for i in range(n * 2):
        a0 = (i / (n * 2)) * 2 * math.pi
        a1 = ((i + 1) / (n * 2)) * 2 * math.pi
        rad = ro if i % 2 == 0 else ri
        pts = [QPointF(cx + rad * math.cos(a0), cy + rad * math.sin(a0)),
               QPointF(cx + rad * math.cos(a1), cy + rad * math.sin(a1))]
        if i == 0:
            path.moveTo(pts[0])
        else:
            path.lineTo(pts[0])
        path.lineTo(pts[1])
    path.closeSubpath()
    hole = QPainterPath()
    hole.addEllipse(QPointF(cx, cy), r.width() * 0.09, r.width() * 0.09)
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    p.drawPath(path.subtracted(hole))


# ---------------------------------------------------------------- widgets
class IconButton(QAbstractButton):
    hovered = Signal(bool)

    def __init__(self, draw: Callable[[QPainter, QRectF], None], parent=None):
        super().__init__(parent)
        self.draw = draw
        self.dot: QColor | None = None
        self.setFixedSize(BTN, BTN)
        self.setCursor(Qt.PointingHandCursor)
        self._hover = False

    def enterEvent(self, e):
        self._hover = True
        self.update()
        self.hovered.emit(True)

    def leaveEvent(self, e):
        self._hover = False
        self.update()
        self.hovered.emit(False)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        if self._hover or self.isDown():
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 70 if self.isDown() else 38))
            p.drawRoundedRect(r, 8, 8)
        self.draw(p, r)
        if self.dot is not None:
            p.setPen(_pen(BAR_BG, 1.5))
            p.setBrush(self.dot)
            p.drawEllipse(QPointF(r.right() - 5, r.bottom() - 5), 3.6, 3.6)
        p.end()


class FloatingBar(QWidget):
    def __init__(self, engine, act: Callable[[str], None], hotkeys: dict, is_hidden: Callable[[], bool],
                 highlight, open_history: Callable[[], None], save_cfg: Callable, pinned: bool = False):
        super().__init__(None, Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
                         | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.engine, self.act, self.is_hidden = engine, act, is_hidden
        self.highlight, self.open_history, self.save_cfg = highlight, open_history, save_cfg
        self.pinned = pinned
        self.hk = {k: "+".join(x.capitalize() if len(x) > 1 else x.upper() for x in v.split("+"))
                   for k, v in hotkeys.items()}
        self.expanded = False
        self.busy = False          # menu open / picking → never auto-collapse
        self._dwell = self._away = 0
        self._anim: QPropertyAnimation | None = None

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 7)
        lay.setSpacing(4)
        self.b_toggle = IconButton(self._draw_toggle)
        self.b_peek = IconButton(lambda p, r: icon_eye(p, r, FG, off=self.is_hidden()))
        self.b_region = IconButton(lambda p, r: icon_region(p, r))
        self.b_reset = IconButton(lambda p, r: icon_reset(p, r, FG))
        self.b_menu = IconButton(lambda p, r: icon_gear(p, r, FG))
        for b in (self.b_toggle, self.b_peek, self.b_region, self.b_reset, self.b_menu):
            lay.addWidget(b)
        self.b_toggle.clicked.connect(lambda: self.act("toggle"))
        self.b_peek.clicked.connect(self._peek)
        self.b_region.clicked.connect(self._region)
        self.b_region.hovered.connect(self._region_hover)
        self.b_reset.clicked.connect(self._reset)
        self.b_menu.clicked.connect(self._menu)
        self.b_peek.setToolTip(f"隱藏／顯示譯文（看原文）　{self.hk.get('peek', '')}")
        self.b_region.setToolTip(f"翻譯範圍：滑鼠停留顯示目前範圍，點一下重新選取　{self.hk.get('region', '')}")
        self.b_reset.setToolTip(f"換作品時用：清除上下文、人名術語、快取　{self.hk.get('reset_context', '')}")
        self.b_menu.setToolTip("設定：翻譯引擎、上下文模式、固定工具列、翻譯記錄、結束")
        self.full_size = self.sizeHint()
        lay.setSizeConstraint(QLayout.SetNoConstraint)  # we size the window ourselves (handle/bar)
        self.setMinimumSize(1, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(50)
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._refresh_status)
        self.status_timer.start(300)
        self._refresh_status()

    # ---------- geometry ----------
    def _screen_geo(self) -> QRect:
        return QGuiApplication.primaryScreen().geometry()

    def _bar_rect(self, shown: bool) -> QRect:
        g = self._screen_geo()
        w, h = self.full_size.width(), self.full_size.height()
        x = g.center().x() - w // 2 + 1
        return QRect(x, g.top() if shown else g.top() - h, w, h)

    def _handle_rect(self) -> QRect:
        g = self._screen_geo()
        return QRect(g.center().x() - HANDLE.width() // 2 + 1, g.top(), HANDLE.width(), HANDLE.height())

    def start(self, show_for_ms: int = 3000):
        """Show expanded briefly so the user learns where it lives, then collapse."""
        self.setGeometry(self._bar_rect(True))
        self.show()
        _set_exstyle(self, add=_WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW)
        _exclude_from_capture(self)
        self.expanded = True
        if not self.pinned:
            QTimer.singleShot(show_for_ms, lambda: (not self._cursor_near()) and self.collapse())

    # ---------- painting ----------
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        if self.expanded or (self._anim is not None):
            p.setBrush(BAR_BG)
            r = QRectF(self.rect()).adjusted(0.5, -12, -0.5, -0.5)  # flush with the top edge
            p.drawRoundedRect(r, 12, 12)
        else:
            p.setBrush(HANDLE_BG)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0, -4, 0, 0), 3, 3)
        p.end()

    def _draw_toggle(self, p: QPainter, r: QRectF):
        (icon_pause if self.engine.enabled else icon_play)(p, r, FG)

    # ---------- expand / collapse ----------
    def expand(self):
        if self.expanded:
            return
        self.expanded = True
        _set_exstyle(self, remove=_WS_EX_TRANSPARENT)
        for b in self.findChildren(IconButton):
            b.show()
        self.setGeometry(self._bar_rect(False))
        self._animate(self._bar_rect(True).topLeft(), None)

    def collapse(self):
        if not self.expanded or self.pinned:
            return
        self.expanded = False
        self.highlight.hide_region()

        def done():
            for b in self.findChildren(IconButton):
                b.hide()
            self.setGeometry(self._handle_rect())
            _set_exstyle(self, add=_WS_EX_TRANSPARENT)  # the handle never blocks clicks
            self.update()

        self._animate(self._bar_rect(False).topLeft(), done)

    def _animate(self, to: QPoint, on_done):
        if self._anim is not None:
            self._anim.stop()
        a = QPropertyAnimation(self, b"pos", self)
        a.setDuration(140)
        a.setEasingCurve(QEasingCurve.OutCubic)
        a.setEndValue(to)

        def fin():
            self._anim = None
            if on_done:
                on_done()
            self.update()

        a.finished.connect(fin)
        self._anim = a
        a.start()

    def _cursor_near(self) -> bool:
        pos = QCursor.pos()
        return self._bar_rect(True).adjusted(-16, -4, 16, 22).contains(pos)

    def _tick(self):
        if self._anim is not None or self.busy:
            return
        pos = QCursor.pos()
        g = self._screen_geo()
        if not self.expanded:
            zone = self._bar_rect(True)
            zone = QRect(zone.left() - 50, g.top(), zone.width() + 100, 3)
            self._dwell = self._dwell + 50 if zone.contains(pos) else 0
            if self._dwell >= DWELL_MS:
                self._dwell = 0
                self.expand()
        elif not self.pinned:
            self._away = 0 if self._cursor_near() else self._away + 50
            if self._away >= LEAVE_MS:
                self._away = 0
                self.collapse()

    # ---------- actions ----------
    def _peek(self):
        self.act("peek")
        QTimer.singleShot(30, self.b_peek.update)

    def _region_hover(self, on: bool):
        if on:
            self.highlight.show_region(self.engine.region)
            self.raise_()  # keep the bar above the (click-through) frame
        else:
            self.highlight.hide_region()

    def _region(self):
        self.act("region")  # main calls begin_pick()/end_pick() around the picker

    def begin_pick(self):
        """Selection mode: get completely out of the way."""
        self.busy = True
        self.highlight.hide_region()
        if self._anim is not None:
            self._anim.stop()
            self._anim = None
        self.hide()

    def end_pick(self):
        self.busy = False
        if self.pinned:
            self.expanded = True
            for b in self.findChildren(IconButton):
                b.show()
            self.setGeometry(self._bar_rect(True))
        else:
            self.expanded = False
            for b in self.findChildren(IconButton):
                b.hide()
            self.setGeometry(self._handle_rect())
        self.show()
        _set_exstyle(self, add=_WS_EX_NOACTIVATE | _WS_EX_TOOLWINDOW
                     | (0 if self.pinned else _WS_EX_TRANSPARENT))
        self.update()

    def _reset(self):
        self.act("reset_context")
        QToolTip.showText(self.b_reset.mapToGlobal(QPoint(0, BTN + 4)), "已清除上下文與人名術語", self.b_reset)

    def _menu(self):
        from .control_panel import ENGINES, MODES
        m = QMenu(self)
        eng = m.addMenu("翻譯引擎")
        g1 = QActionGroup(m)
        for k, label in ENGINES:
            a = QAction(label, m, checkable=True, checked=(self.engine.c["engine"] == k))
            a.triggered.connect(lambda _=False, k=k: self._set_engine(k))
            g1.addAction(a)
            eng.addAction(a)
        mode = m.addMenu("上下文模式")
        mode.setEnabled(self.engine.c["engine"] == "llm")
        g2 = QActionGroup(m)
        for k, label in MODES:
            a = QAction(label, m, checkable=True, checked=(self.engine.c["context"]["mode"] == k))
            a.triggered.connect(lambda _=False, k=k: self._set_mode(k))
            g2.addAction(a)
            mode.addAction(a)
        m.addSeparator()
        pin = QAction("固定顯示工具列", m, checkable=True, checked=self.pinned)
        pin.triggered.connect(self._set_pin)
        m.addAction(pin)
        a = QAction("翻譯記錄與狀態…", m)
        a.triggered.connect(self.open_history)
        m.addAction(a)
        m.addSeparator()
        a = QAction(f"結束程式　{self.hk.get('quit', '')}", m)
        a.triggered.connect(lambda: self.act("quit"))
        m.addAction(a)
        self.busy = True
        try:
            m.exec(self.b_menu.mapToGlobal(QPoint(0, BTN + 2)))
        finally:
            self.busy = False

    def _set_engine(self, k):
        self.save_cfg(engine=k)
        self.engine.switch_engine(k)

    def _set_mode(self, k):
        self.engine.c["context"]["mode"] = k
        self.save_cfg(context={"mode": k})

    def _set_pin(self, on: bool):
        self.pinned = on
        self.save_cfg(ui={"pin_toolbar": on})
        if on:
            self.expand()

    # ---------- status ----------
    def _refresh_status(self):
        e = self.engine
        s = e.stats
        if e.error:
            key, text = "error", "錯誤：" + e.error
        elif not e.ready.is_set():
            key, text = "load", "翻譯模型載入中…"
        elif not e.enabled:
            key, text = "pause", "已暫停（點一下開始）"
        else:
            key, text = "run", "翻譯中（點一下暫停）"
        self.b_toggle.dot = STATUS[key]
        eng = "LLM" if e.c["engine"] == "llm" else "非 LLM"
        self.b_toggle.setToolTip(f"{text}　{self.hk.get('toggle', '')}\n引擎：{eng}｜畫面上 {len(e.blocks)} 段"
                                 f"｜OCR {s['ocr_ms']:.0f} ms｜上次翻譯 {s['tr_ms'] / 1000:.2f} 秒")
        self.b_toggle.update()
        self.b_peek.update()
