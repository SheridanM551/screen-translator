"""Toolbar checks: icon sheet + expanded bar PNGs, expand/collapse geometry, click-through
when collapsed, capture exclusion, hide during region picking. Exits by itself."""
import ctypes
import os
import sys
import threading
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from PySide6.QtCore import QRectF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

OUT = os.path.join(ROOT, "tests", "out")


class StubHighlight:
    def __init__(self):
        self.calls = []

    def show_region(self, r):
        self.calls.append(("show", r))

    def hide_region(self):
        self.calls.append(("hide",))


def main():
    app = QApplication(sys.argv)
    from app import toolbar as tb
    os.makedirs(OUT, exist_ok=True)

    # icon sheet at 3x on dark background
    S = 34 * 3
    names = ["play", "pause", "eye", "eye-off", "region", "reset", "gear"]
    img = QImage(S * len(names), S, QImage.Format_ARGB32)
    img.fill(tb.BAR_BG)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    for i, n in enumerate(names):
        p.save()
        p.translate(i * S, 0)
        p.scale(3, 3)
        r = QRectF(2, 2, 30, 30)
        {"play": lambda: tb.icon_play(p, r, tb.FG), "pause": lambda: tb.icon_pause(p, r, tb.FG),
         "eye": lambda: tb.icon_eye(p, r, tb.FG), "eye-off": lambda: tb.icon_eye(p, r, tb.FG, True),
         "region": lambda: tb.icon_region(p, r), "reset": lambda: tb.icon_reset(p, r, tb.FG),
         "gear": lambda: tb.icon_gear(p, r, tb.FG)}[n]()
        p.restore()
    p.end()
    img.save(os.path.join(OUT, "toolbar_icons.png"))

    eng = types.SimpleNamespace(enabled=True, ready=threading.Event(), error=None,
                                stats={"ocr_ms": 150, "tr_ms": 620}, blocks={}, region=None,
                                c={"engine": "llm", "context": {"mode": "refine"}},
                                switch_engine=lambda k: None)
    eng.ready.set()
    acts = []
    hl = StubHighlight()
    bar = tb.FloatingBar(eng, acts.append, {"toggle": "ctrl+alt+t", "peek": "ctrl+alt+h",
                                            "region": "ctrl+alt+r", "reset_context": "ctrl+alt+c",
                                            "quit": "ctrl+alt+q"},
                         lambda: False, hl, lambda: None, lambda **kw: None)
    bar.start(show_for_ms=10_000)
    u32 = ctypes.windll.user32
    u32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    results = []

    def check(label, cond):
        results.append(bool(cond))
        print(("PASS " if cond else "FAIL ") + label)

    def exstyle():
        return u32.GetWindowLongPtrW(ctypes.c_void_p(int(bar.winId())), -20)

    def step1():
        bar.grab().save(os.path.join(OUT, "toolbar_expanded.png"))
        g = bar.geometry()
        sg = app.primaryScreen().geometry()
        check(f"expanded at top centre {g.getRect()} (screen {sg.getRect()})",
              g.top() == sg.top() and abs(g.center().x() - sg.center().x()) <= 2)
        aff = ctypes.c_uint32()
        u32.GetWindowDisplayAffinity(int(bar.winId()), ctypes.byref(aff))
        check(f"excluded from capture (affinity=0x{aff.value:x})", aff.value == 0x11)
        check("no-activate tool window", exstyle() & 0x08000000 and exstyle() & 0x80)
        check("expanded bar is clickable (not transparent)", not exstyle() & 0x20)
        check(f"bar is small: {g.width()}x{g.height()} logical", g.width() <= 240 and g.height() <= 50)
        bar.b_region.hovered.emit(True)
        bar.b_region.hovered.emit(False)
        check("hovering region icon shows/hides the range", hl.calls[-2:] == [("show", None), ("hide",)])
        bar.b_toggle.click()
        check("toggle icon fires action", acts[-1] == "toggle")
        bar.collapse()
        QTimer.singleShot(400, step2)

    def step2():
        g = bar.geometry()
        check(f"collapsed to a small handle {g.width()}x{g.height()}", g.width() <= 60 and g.height() <= 8)
        check("handle is click-through", exstyle() & 0x20)
        bar.expand()
        QTimer.singleShot(400, step3)

    def step3():
        g = bar.geometry()
        check("expands again", g.height() > 20 and not exstyle() & 0x20)
        bar.begin_pick()
        check("hidden while picking a region", not bar.isVisible())
        bar.end_pick()
        g = bar.geometry()
        check("back to handle after picking", bar.isVisible() and g.height() <= 8 and exstyle() & 0x20)
        print("ALL PASS" if all(results) else "SOME FAILED")
        app.quit()

    QTimer.singleShot(600, step1)
    QTimer.singleShot(8000, app.quit)
    app.exec()


if __name__ == "__main__":
    main()
