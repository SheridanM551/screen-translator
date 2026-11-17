"""Overlay demo + self-test.

1. Offscreen: renders sample blocks over tests/samples/{manga,game}.png and saves
   tests/out/overlay_demo.png (manga panel on top, game panel below).
2. Live (skip with --offscreen): shows the overlay for ~3 s on the primary monitor and checks
   a) capture exclusion  - coloured probe patches must NOT appear in an mss grab
   b) DPI placement      - with exclusion temporarily off, probe patches must land on
                           exactly the expected physical pixels
   c) peek / remove      - set_hidden(True) and remove_blocks() really clear the pixels
   Prints PASS/FAIL per check, exits 0 only if everything passed. Never enters app.exec().
"""
from __future__ import annotations

import argparse
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

# QApplication must exist before the first mss instance (see notes in app/overlay.py docstring)
from PySide6.QtCore import QEventLoop, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import numpy as np  # noqa: E402

from app import overlay as ov  # noqa: E402
from app.overlay import Overlay, RenderBlock, paint_block  # noqa: E402

OUT_DIR = os.path.join(ROOT, "tests", "out")
SAMPLES = os.path.join(ROOT, "tests", "samples")
W, H = 1920, 1200


# ----------------------------------------------------------------------------- samples
def manga_blocks() -> list[RenderBlock]:
    """Source rects match the vertical bubbles drawn by tools/make_samples.py."""
    white = (255, 255, 255)
    return [
        RenderBlock("m1", (260, 147, 330, 374), "你這傢伙，到底是什麼人？", True, white, None, 30),
        RenderBlock("m2", (685, 190, 746, 471), "那種事情，根本無所謂吧。", True, white, None, 26),
        RenderBlock("m3", (1213, 119, 1278, 421), "今天已經很晚了，明天再說吧。", True, white, None, 28),
        RenderBlock("m4", (1640, 170, 1750, 430), "等一下！話還沒說完啊！", True, white, None, 30),
        RenderBlock("m5", (468, 786, 524, 994), "呵呵，你果然很有趣呢。", True, white, None, 24),
        RenderBlock("m6", (1385, 776, 1504, 984), "聽說前方有危險的魔物。", True, white, None, 32),
    ]


def game_blocks(bg_of) -> list[RenderBlock]:
    """bg_of(rect) samples the background like the main loop would."""
    b = [
        ("g_name", (170, 781, 272, 815), "愛麗絲", False, 34),
        ("g_dlg", (180, 872, 1064, 970),
         "「欸，這條路真的走對了嗎？從剛才開始就覺得一直在同一個地方打轉……」", False, 34),
        ("g_save", (1100, 1102, 1166, 1126), "存檔", False, 22),
        ("g_load", (1210, 1102, 1276, 1126), "讀取", False, 22),
        ("g_auto", (1320, 1102, 1386, 1126), "自動", False, 22),
        ("g_skip", (1430, 1102, 1518, 1126), "略過", False, 22),
        ("g_log", (1540, 1102, 1584, 1126), "紀錄", False, 22),
        ("g_cfg", (1650, 1102, 1694, 1126), "設定", False, 22),
        # stress cases on the sky area
        ("x_long", (200, 150, 330, 190),
         "這是一段非常長的翻譯文字，被硬塞進一個很小的框框裡面，看看會發生什麼事情。", False, 20),
        ("x_latin", (600, 150, 1100, 200), "HP回復了120點！Level Up！獲得「Excalibur」×1", False, 30),
        ("x_vlatin", (1500, 80, 1560, 560), "這把劍要價500金幣，OK嗎？真的假的!?Excalibur耶～", True, 28),
        ("x_tiny", (1300, 300, 1322, 316), "是", False, 0),
        ("x_tiny2", (1360, 300, 1400, 314), "確定嗎", False, 14),
        ("x_vlong", (1700, 100, 1740, 260),
         "這段直排文字太長了，框框卻很窄，所以必須往左邊延伸才能全部放進去喔。", True, 30),
        ("x_multi", (300, 400, 900, 560), "第一行：測試。\n第二行比較長一點，看看置中與換行的效果如何。", False, 40),
        # cannot fit even at the minimum size -> patch grows (H: downward, V: leftward)
        ("x_ovf_h", (200, 240, 300, 256), "空間嚴重不足的橫排文字，會往下方延伸補丁。", False, 0),
        ("x_ovf_v", (1840, 100, 1856, 200), "空間嚴重不足的直排文字，會往左方延伸補丁。", True, 0),
    ]
    return [RenderBlock(i, r, t, v, bg_of(r), None, c) for i, r, t, v, c in b]


def ring_color(img: np.ndarray, rect) -> tuple[int, int, int]:
    """Median colour of a 3-px ring just outside rect (img is HxWx3 RGB)."""
    x0, y0, x1, y1 = rect
    h, w = img.shape[:2]
    xa, ya, xb, yb = max(0, x0 - 6), max(0, y0 - 6), min(w, x1 + 6), min(h, y1 + 6)
    patch = img[ya:yb, xa:xb].reshape(-1, 3)
    return tuple(int(v) for v in np.median(patch, axis=0))


def load_qimage_rgb(img: QImage) -> np.ndarray:
    q = img.convertToFormat(QImage.Format_RGB888)
    a = np.frombuffer(q.constBits(), np.uint8).reshape(q.height(), q.bytesPerLine())
    return a[:, : q.width() * 3].reshape(q.height(), q.width(), 3).copy()


def load_rgb(path: str) -> np.ndarray:
    return load_qimage_rgb(QImage(path))


def save_strip(crops: list[np.ndarray], path: str) -> None:
    """Save patch-only crops (no desktop content) side by side."""
    h = max(c.shape[0] for c in crops)
    w = sum(c.shape[1] for c in crops) + 8 * (len(crops) - 1)
    canvas = np.full((h, w, 3), 128, np.uint8)
    x = 0
    for c in crops:
        canvas[: c.shape[0], x: x + c.shape[1]] = c
        x += c.shape[1] + 8
    canvas = np.ascontiguousarray(canvas)
    QImage(canvas.data, w, h, 3 * w, QImage.Format_RGB888).copy().save(path)


# ----------------------------------------------------------------------------- offscreen
def render_offscreen(debug: bool) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    manga = QImage(os.path.join(SAMPLES, "manga.png")).convertToFormat(QImage.Format_ARGB32_Premultiplied)
    game = QImage(os.path.join(SAMPLES, "game.png")).convertToFormat(QImage.Format_ARGB32_Premultiplied)
    game_rgb = load_rgb(os.path.join(SAMPLES, "game.png"))
    mb = manga_blocks()
    gb = game_blocks(lambda r: ring_color(game_rgb, r))

    canvas = QImage(W, 2 * H, QImage.Format_ARGB32_Premultiplied)
    p = QPainter(canvas)
    p.drawImage(0, 0, manga)
    p.drawImage(0, H, game)
    t0 = time.perf_counter()
    for b in mb:
        paint_block(p, b, 1.0, (0, 0, W, H))
    p.translate(0, H)
    for b in gb:
        paint_block(p, b, 1.0, (0, 0, W, H))
    cold = (time.perf_counter() - t0) * 1000
    if debug:  # source rects (thin red) to judge the fit
        p.setPen(QPen(QColor(255, 0, 0, 160), 1))
        p.setBrush(Qt.NoBrush)
        for b in gb:
            x0, y0, x1, y1 = b.rect
            p.drawRect(x0, y0, x1 - x0 - 1, y1 - y0 - 1)
        p.translate(0, -H)
        for b in mb:
            x0, y0, x1, y1 = b.rect
            p.drawRect(x0, y0, x1 - x0 - 1, y1 - y0 - 1)
    p.end()
    out = os.path.join(OUT_DIR, "overlay_demo.png" if not debug else "overlay_demo_debug.png")
    canvas.save(out)
    print(f"offscreen: {len(mb) + len(gb)} blocks rendered (cold, incl. layout) in {cold:.1f} ms -> {out}")

    # warm timing: 20 blocks, layouts cached, pure vector painting
    blocks = (mb + gb)[:20]
    img = QImage(W, H, QImage.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    for b in blocks:
        paint_block(p, b, 1.0)
    t0 = time.perf_counter()
    reps = 20
    for _ in range(reps):
        for b in blocks:
            paint_block(p, b, 1.0)
    p.end()
    warm = (time.perf_counter() - t0) * 1000 / reps
    print(f"offscreen: paint_block x{len(blocks)} with cached layouts: {warm:.2f} ms/frame")


# ----------------------------------------------------------------------------- live
def live_blocks() -> list[RenderBlock]:
    """Manga + game samples on one screen, dropping game blocks that would overlap."""
    mb = manga_blocks()

    def hits(a, b):
        return a[0] < b[2] + 8 and b[0] < a[2] + 8 and a[1] < b[3] + 8 and b[1] < a[3] + 8
    gb = [g for g in game_blocks(lambda r: (40, 40, 60)) if not any(hits(g.rect, m.rect) for m in mb)]
    return mb + gb


def pump(app: QApplication, ms: float) -> None:
    end = time.perf_counter() + ms / 1000.0
    while time.perf_counter() < end:
        app.processEvents(QEventLoop.AllEvents, 5)
        time.sleep(0.004)


def grab(sct, mon) -> np.ndarray:
    s = sct.grab(mon)
    return np.frombuffer(s.raw, np.uint8).reshape(s.height, s.width, 4)[:, :, :3][:, :, ::-1]  # RGB


def color_bbox(img: np.ndarray, color, near, margin=24):
    """Bounding box (x0, y0, x1, y1 exclusive) of pixels == color within near +- margin."""
    x0, y0, x1, y1 = near
    h, w = img.shape[:2]
    xa, ya, xb, yb = max(0, x0 - margin), max(0, y0 - margin), min(w, x1 + margin), min(h, y1 + margin)
    sub = img[ya:yb, xa:xb].astype(np.int16)
    m = np.all(np.abs(sub - np.array(color, np.int16)) <= 2, axis=2)
    if not m.any():
        return None
    ys, xs = np.nonzero(m)
    return xa + int(xs.min()), ya + int(ys.min()), xa + int(xs.max()) + 1, ya + int(ys.max()) + 1


def run_live(app: QApplication) -> bool:
    import mss  # created only after QApplication (DPI awareness ordering)

    results: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))

    with mss.MSS() as sct:
        mon = sct.monitors[1]
        mrect = (mon["left"], mon["top"], mon["width"], mon["height"])
        mw, mh = mon["width"], mon["height"]
        print(f"live: monitor {mrect}")
        base = grab(sct, mon)

        o = Overlay(mrect)
        # probes: pure colours, no text; odd coordinates so they straddle logical pixels
        probes = [
            RenderBlock("p_red", (101, 203, 178, 251), "", False, (255, 0, 0)),
            RenderBlock("p_green", (min(1001, mw - 200), min(703, mh - 120),
                                    min(1078, mw - 120), min(741, mh - 80)), "", False, (0, 255, 0)),
            RenderBlock("p_blue", (mw - 71, mh - 49, mw - 8, mh - 9), "", False, (0, 0, 255)),
        ]
        blocks = live_blocks() + probes
        o.set_blocks(blocks)
        pump(app, 400)

        dpr = o.devicePixelRatioF()
        nat = o.native_rect()
        g = o.geometry()
        print(f"  devicePixelRatio={dpr}  logical geometry=({g.x()},{g.y()},{g.width()},{g.height()})"
              f"  native HWND rect={nat}")
        exs = ov._user32.GetWindowLongPtrW(o.hwnd, ov.GWL_EXSTYLE)
        need = ov.WS_EX_TRANSPARENT | ov.WS_EX_LAYERED | ov.WS_EX_NOACTIVATE | ov.WS_EX_TOOLWINDOW
        check("window ex-styles (TRANSPARENT|LAYERED|NOACTIVATE|TOOLWINDOW)", (exs & need) == need,
              f"exstyle=0x{exs:08x}")
        check("native window covers monitor exactly", nat == mrect, f"{nat} vs {mrect}")
        aff = o.display_affinity()
        check("display affinity = WDA_EXCLUDEFROMCAPTURE", aff == ov.WDA_EXCLUDEFROMCAPTURE,
              f"GetWindowDisplayAffinity -> 0x{aff:x}")
        check("overlay did not take focus",
              ov._user32.GetForegroundWindow() != o.hwnd)

        # full-repaint timing with ~25 cached patches
        o.repaint()
        check("paintEvent fast (< 5 ms)", o.last_paint_ms < 5.0,
              f"{o.last_paint_ms:.2f} ms for {len(blocks)} blocks (full window)")

        # (a) capture exclusion
        img = grab(sct, mon)
        leaks = [p.id for p in probes if color_bbox(img, p.bg, p.rect, 4) is not None
                 and color_bbox(base, p.bg, p.rect, 4) is None]
        check("capture exclusion (probes invisible to mss)", not leaks,
              f"visible: {leaks}" if leaks else "no probe pixels in grab")

        # (b) DPI placement: make the overlay capturable for a moment
        o.set_capture_excluded(False)
        pump(app, 350)
        img = grab(sct, mon)
        for p in probes:
            x0, y0, x1, y1 = p.rect
            exp = (x0 - ov.PAD, y0 - ov.PAD, x1 + ov.PAD, y1 + ov.PAD)
            got = color_bbox(img, p.bg, p.rect)
            check(f"DPI placement {p.id}", got == exp, f"expected {exp}, got {got}")
        # text patches: live pixels must equal the offscreen rasterisation 1:1 (no resampling)
        tl = o.to_logical()
        diffs = []
        crops = []
        for b in [blk for blk in blocks if not blk.id.startswith("p_")]:
            pimg, px, py = ov.render_block_image(b, tl, (0, 0, mw, mh))
            ref = load_qimage_rgb(pimg)
            h, w = ref.shape[:2]
            live = img[py:py + h, px:px + w]
            r = int(ov.RADIUS_MAX) + 1           # skip anti-aliased corners (blend with desktop)
            d = np.abs(live[r:h - r, r:w - r].astype(int) - ref[r:h - r, r:w - r].astype(int))
            diffs.append((b.id, float(d.mean()), int(d.max())))
            crops.append(live)
        worst = max(diffs, key=lambda t: t[1])
        check("live patches == offscreen render (pixel-exact placement & scale)", worst[1] < 1.0,
              "mean|diff| per block: " + ", ".join(f"{i}={m:.2f}" for i, m, _ in diffs))
        save_strip(crops, os.path.join(OUT_DIR, "overlay_live_patches.png"))

        # (c) peek toggle and remove
        o.set_hidden(True)
        pump(app, 300)
        img = grab(sct, mon)
        vis = [p.id for p in probes if color_bbox(img, p.bg, p.rect, 4) is not None]
        check("set_hidden(True) clears the screen", not vis, f"still visible: {vis}" if vis else "")
        o.set_hidden(False)
        o.remove_blocks(["p_green"])
        pump(app, 300)
        img = grab(sct, mon)
        red_back = color_bbox(img, (255, 0, 0), probes[0].rect, 4) is not None
        green_gone = color_bbox(img, (0, 255, 0), probes[1].rect, 4) is None
        check("set_hidden(False) restores, remove_blocks erases", red_back and green_gone,
              f"red visible={red_back}, green gone={green_gone}")

        # restore exclusion and confirm again
        o.set_capture_excluded(True)
        pump(app, 350)
        img = grab(sct, mon)
        leaks = [p.id for p in probes if color_bbox(img, p.bg, p.rect, 4) is not None
                 and color_bbox(base, p.bg, p.rect, 4) is None]
        check("capture exclusion restored", not leaks, f"visible: {leaks}" if leaks else "")

        # keep it on screen for a while so a human can look at it, then exit cleanly
        o.set_blocks(live_blocks())
        pump(app, 1500)
        o.close()
        o.deleteLater()
        pump(app, 100)

    ok = all(r[1] for r in results)
    print(f"live: {'ALL PASS' if ok else 'SOME CHECKS FAILED'} ({sum(r[1] for r in results)}/{len(results)})")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offscreen", action="store_true", help="only render the PNG, no live window")
    args = ap.parse_args()
    app = QApplication.instance() or QApplication(sys.argv)
    render_offscreen(debug=False)
    render_offscreen(debug=True)
    ok = True
    if not args.offscreen:
        ok = run_live(app)
    app.processEvents()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
