"""Region highlight + region picker demo / self-test.

1. Offscreen PNGs in tests/out/ (rendered through the real widgets' paintEvent via grab()):
   region_highlight_sub.png   reveal at 3 animation stages, sub-region (over manga sample)
   region_highlight_full.png  same for the whole monitor (over game sample)
   region_picker_idle.png     picker idle: hint pill + current-region frame (sub-region)
   region_picker_idle_full.png  idle with current region = whole monitor
   region_picker_drag.png     picker mid-drag (+ a drag running under the hint)
2. Live (skip with --offscreen): highlight show/hide cycles + jitter, capture exclusion and
   placement checks, then the picker driven with injected input (Space -> "full",
   Esc -> None, drag -> rect). Input is only injected when the picker is verifiably the
   window under the cursor / in the foreground. Exits by itself; exit code 0 = all PASS.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import random
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from PySide6.QtCore import QEventLoop, QRectF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import numpy as np  # noqa: E402

from app import region_picker as rp  # noqa: E402
from app.region_highlight import RegionHighlight  # noqa: E402

OUT = os.path.join(ROOT, "tests", "out")
SAMPLES = os.path.join(ROOT, "tests", "samples")
SUB = (360, 140, 1480, 1000)   # physical, monitor-relative


# ----------------------------------------------------------------------------- offscreen
def _bg(name: str, w: int, h: int) -> QImage:
    img = QImage(os.path.join(SAMPLES, name))
    if img.isNull():
        img = QImage(w, h, QImage.Format_RGB32)
        img.fill(QColor(90, 110, 140))
    return img.convertToFormat(QImage.Format_ARGB32_Premultiplied).scaled(w, h)


def _compose(bg: QImage, pm: QPixmap) -> QImage:
    out = bg.copy()
    pm.setDevicePixelRatio(1.0)                 # draw physical pixels 1:1
    p = QPainter(out)
    p.drawPixmap(0, 0, pm)
    p.end()
    return out


def _strip(images: list[QImage], captions: list[str], scale: float = 0.5) -> QImage:
    w = int(images[0].width() * scale)
    h = int(images[0].height() * scale)
    cap = 34
    out = QImage(len(images) * w + (len(images) - 1) * 12, h + cap, QImage.Format_RGB32)
    out.fill(QColor(40, 40, 40))
    p = QPainter(out)
    p.setRenderHint(QPainter.SmoothPixmapTransform, True)
    f = QFont()
    f.setFamilies(["Microsoft JhengHei UI", "Segoe UI"])
    f.setPixelSize(18)
    p.setFont(f)
    for i, (img, c) in enumerate(zip(images, captions)):
        x = i * (w + 12)
        p.drawImage(QRectF(x, cap, w, h), img)
        p.setPen(QColor(230, 230, 230))
        p.drawText(QRectF(x, 0, w, cap), Qt.AlignCenter, c)
    p.end()
    return out


def render_offscreen(mon: tuple[int, int, int, int]) -> list[str]:
    os.makedirs(OUT, exist_ok=True)
    _, _, mw, mh = mon
    manga, game = _bg("manga.png", mw, mh), _bg("game.png", mw, mh)
    saved = []

    hl = RegionHighlight(mon)
    for name, rect, bg in (("region_highlight_sub.png", SUB, manga),
                           ("region_highlight_full.png", None, game)):
        hl._rect = hl._norm(rect)
        frames, caps = [], []
        for t in (0.12, 0.45, 1.0):
            hl._render_state(t, t)
            frames.append(_compose(bg, hl.grab()))
            caps.append(f"t = {int(t * 300)} ms")
        path = os.path.join(OUT, name)
        _strip(frames, caps).save(path)
        frames[-1].copy(0, 0, 700, 420).save(path.replace(".png", "_corner.png"))
        saved += [path, path.replace(".png", "_corner.png")]
    hl.deleteLater()

    for name, cur, bg in (("region_picker_idle.png", SUB, manga),
                          ("region_picker_idle_full.png", None, game)):
        pk = rp.RegionPicker(lambda r: None, mon, cur)
        img = _compose(bg, pk.grab())
        path = os.path.join(OUT, name)
        img.save(path)
        saved.append(path)
        pk._done = True
        pk.deleteLater()

    pk = rp.RegionPicker(lambda r: None, mon, SUB)
    pk._dragging = True
    pk._p0, pk._p1 = (520, 260), (1290, 760)
    a = _compose(manga, pk.grab())
    pk._p0, pk._p1 = (1100, 700), (mw - 40, mh - 20)
    b = _compose(manga, pk.grab())
    path = os.path.join(OUT, "region_picker_drag.png")
    _strip([a, b], ["拖曳中", "拖曳經過提示框（提示變淡）"]).save(path)
    saved.append(path)
    pk._done = True
    pk.deleteLater()
    return saved


# ----------------------------------------------------------------------------- live
_u = ctypes.WinDLL("user32")
_u.WindowFromPoint.argtypes = [wt.POINT]
_u.WindowFromPoint.restype = wt.HWND
_u.GetAncestor.argtypes = [wt.HWND, ctypes.c_uint]
_u.GetAncestor.restype = wt.HWND
_u.GetForegroundWindow.restype = wt.HWND


def pump(app, ms: float) -> None:
    end = time.perf_counter() + ms / 1000.0
    while time.perf_counter() < end:
        app.processEvents(QEventLoop.AllEvents, 5)
        time.sleep(0.003)


def grab(sct, mon) -> np.ndarray:
    s = sct.grab(mon)
    return np.frombuffer(s.raw, np.uint8).reshape(s.height, s.width, 4)[:, :, 2::-1]  # RGB


def is_red(px) -> bool:
    return int(px[0]) >= 250 and int(px[1]) <= 5 and int(px[2]) <= 5


def red_count(img: np.ndarray, box) -> int:
    x0, y0, x1, y1 = box
    sub = img[max(0, y0):y1, max(0, x0):x1].astype(int)
    return int(((sub[:, :, 0] >= 250) & (sub[:, :, 1] <= 5) & (sub[:, :, 2] <= 5)).sum())


def run_live(app) -> bool:
    import mss

    res: list[bool] = []

    def check(name, ok, detail=""):
        res.append(bool(ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))

    with mss.MSS() as sct:
        m = sct.monitors[1]
        mon = (m["left"], m["top"], m["width"], m["height"])
        mw, mh = m["width"], m["height"]
        dpr = app.primaryScreen().devicePixelRatio()
        lw = max(2, int(round(3 * dpr)))
        print(f"live: monitor {mon}, dpr {dpr}, frame line {lw} px")

        # ---------------- highlight
        hl = RegionHighlight(mon)
        A = (301, 203, 1001, 703)
        band = (A[0] - lw - 3, A[1] - lw - 3, A[2] + lw + 3, A[3] + lw + 3)
        base = grab(sct, m)
        t0 = time.perf_counter()
        hl.show_region(A)
        pump(app, 30)
        mid_alpha, mid_grow = hl._alpha, hl._grow
        pump(app, 420)
        check("show_region animates to completion (~300 ms) and stops its timer",
              0 < mid_alpha < 1 and hl._alpha == 1 and hl._grow == 1 and not hl._timer.isActive(),
              f"after 30 ms alpha={mid_alpha:.2f}; end alpha={hl._alpha} grow={hl._grow}")
        check("highlight never takes focus", _u.GetForegroundWindow() != hl._hwnd)
        check("display affinity = WDA_EXCLUDEFROMCAPTURE", hl.display_affinity() == 0x11,
              f"0x{hl.display_affinity():x}")
        img = grab(sct, m)
        added = red_count(img, band) - red_count(base, band)
        check("capture exclusion (red frame absent from mss grab)", added <= 0,
              f"{added} new pure-red px in frame area")

        hl.set_capture_excluded(False)
        pump(app, 150)
        img = grab(sct, m)
        y_mid, x_mid = (A[1] + A[3]) // 2, (A[0] + A[2]) // 2
        left_ok = all(is_red(img[y_mid, x]) for x in range(A[0] - lw, A[0]))
        top_ok = all(is_red(img[y, x_mid]) for y in range(A[1] - lw, A[1]))
        inside_clear = not is_red(img[y_mid, A[0]]) or is_red(base[y_mid, A[0]])
        check("frame visible when exclusion is off, band exactly outside the rect",
              left_ok and top_ok and inside_clear,
              f"left band x={A[0] - lw}..{A[0] - 1} red={left_ok}, top band red={top_ok}, "
              f"rect pixel untouched={inside_clear}")

        hl.show_region(None)                     # whole monitor, re-reveal
        pump(app, 450)
        img = grab(sct, m)
        edges = [img[mh // 2, 1], img[mh // 2, mw - 2], img[1, mw // 2], img[mh - 2, mw // 2]]
        check("full-screen frame is inset and visible on all 4 edges",
              all(is_red(px) for px in edges), str([list(map(int, px)) for px in edges]))
        hl.set_capture_excluded(True)
        pump(app, 150)
        img = grab(sct, m)
        added = red_count(img, (0, 0, mw, 12)) - red_count(base, (0, 0, mw, 12))
        check("capture exclusion restored", added <= 0, f"{added} new red px on top edge")

        hl.hide_region()
        pump(app, 60)
        fading = hl.isVisible() and 0 < hl._alpha < 1
        pump(app, 200)
        check("hide_region fades (~150 ms) then hides", fading and not hl.isVisible()
              and not hl._timer.isActive(), f"mid-fade seen={fading}")

        # jitter: rapid interleaved show/hide/show (mouse wobbling over the toolbar icon)
        rnd = random.Random(7)
        n_timers = len(hl.findChildren(QTimer))
        errors = []
        try:
            for i in range(80):
                if rnd.random() < 0.5:
                    hl.show_region(A if rnd.random() < 0.7 else None)
                else:
                    hl.hide_region()
                pump(app, rnd.uniform(0, 25))
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))
        hl.hide_region()
        pump(app, 300)
        settled_hidden = not hl.isVisible() and not hl._timer.isActive()
        hl.show_region(A)
        pump(app, 450)
        settled_shown = hl.isVisible() and hl._alpha == 1 and hl._grow == 1
        check("80 random show/hide calls: no errors, no extra timers, settles cleanly",
              not errors and settled_hidden and settled_shown
              and len(hl.findChildren(QTimer)) == n_timers,
              f"errors={errors} hidden_after={settled_hidden} shown_after={settled_shown} "
              f"timers={len(hl.findChildren(QTimer))}")
        hl.hide_region()
        pump(app, 250)

        # ---------------- picker
        def picker():
            return next(iter(rp._active), None)

        def guarded_keys(vk, label):
            pk = picker()
            if pk is None:
                return False
            if _u.GetForegroundWindow() != int(pk.winId()):
                print(f"    (skip {label}: picker is not foreground)")
                pk.finish(None)
                return False
            _u.keybd_event(vk, 0, 0, 0)
            _u.keybd_event(vk, 0, 2, 0)
            return True

        got = []
        rp.pick_region(lambda r: got.append(r), mon, current_region=A)
        pump(app, 500)
        pk = picker()
        check("picker takes the foreground", pk is not None
              and _u.GetForegroundWindow() == int(pk.winId()))
        img = grab(sct, m)
        corner_red = all(is_red(img[A[1] - lw // 2 - 1, x]) for x in range(A[0] - lw, A[0] + 10))
        hx, hy = mw - int(30 * dpr), mh - int(30 * dpr)     # inside hint pill padding
        hint_dark = int(img[hy, hx].max()) < 70
        check("picker idle: current-region brackets at exact pixels + bottom-right hint",
              corner_red and hint_dark,
              f"bracket red={corner_red}, hint px{(hx, hy)}={list(map(int, img[hy, hx]))}")
        guarded_keys(0x20, "Space")
        pump(app, 300)
        check("Space -> 'full'", got == ["full"], f"got {got}")

        got = []
        rp.pick_region(lambda r: got.append(r), mon)
        pump(app, 500)
        guarded_keys(0x1B, "Esc")
        pump(app, 300)
        check("Esc -> None", got == [None], f"got {got}")

        got = []
        rp.pick_region(lambda r: got.append(r), mon, current_region=None)
        pump(app, 500)
        pk = picker()
        under = _u.GetAncestor(_u.WindowFromPoint(wt.POINT(400, 300)), 2)
        if pk is not None and under == int(pk.winId()):
            # cursor is re-positioned right before each button event so a user nudging the
            # real mouse during the test cannot shift the press/release points
            _u.SetCursorPos(400, 300)
            _u.mouse_event(0x2, 0, 0, 0, 0)
            pump(app, 40)
            for i in range(1, 11):
                _u.SetCursorPos(400 + 40 * i, 300 + 30 * i)
                pump(app, 20)
            pump(app, 120)
            img = grab(sct, m)
            sel_lw = max(2, int(round(2 * dpr)))
            frame_ok = all(is_red(img[450, x]) for x in range(400 - sel_lw, 400))
            _u.SetCursorPos(800, 600)
            _u.mouse_event(0x4, 0, 0, 0, 0)
            pump(app, 300)
            check("drag -> rect (400, 300, 801, 601), red selection frame just outside",
                  got == [(400, 300, 801, 601)] and frame_ok, f"got {got}, frame red={frame_ok}")
        else:
            if pk is not None:
                pk.finish(None)
            pump(app, 200)
            check("drag -> rect", False, "skipped: picker not under the cursor")

        hl.close()
        hl.deleteLater()
        pump(app, 100)

    ok = all(res)
    print(f"live: {'ALL PASS' if ok else 'SOME CHECKS FAILED'} ({sum(res)}/{len(res)})")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offscreen", action="store_true")
    args = ap.parse_args()
    app = QApplication.instance() or QApplication(sys.argv)
    scr = app.primaryScreen()
    g, d = scr.geometry(), scr.devicePixelRatio()
    mon = (g.x(), g.y(), round(g.width() * d), round(g.height() * d))
    for pth in render_offscreen(mon):
        print("wrote", pth)
    ok = True
    if not args.offscreen:
        ok = run_live(app)
    app.processEvents()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
