"""Live end-to-end run on the real screen: shows a sample page fullscreen, runs the real
Engine (capture → OCR → grouping → translation → overlay), then screenshots the result.

  python tools/e2e_live.py [nmt|llm]     (default nmt; llm needs llama-server allowed to start)

Screenshots: tests/out/e2e_<sample>.png (overlay is briefly made capturable for the grab).
"""
import os
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

from app import config as cfgmod  # noqa: E402
from app.main import Engine, setup_logging  # noqa: E402


def main():
    engine_kind = sys.argv[1] if len(sys.argv) > 1 else "nmt"
    setup_logging()
    app = QApplication(sys.argv)
    import mss
    import numpy as np
    from PIL import Image

    from app.overlay import Overlay

    c = cfgmod.load()
    c["engine"] = engine_kind
    c["region"] = None
    with mss.MSS() as s:
        mon = dict(s.monitors[1])
    view = QLabel()
    view.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
    view.setStyleSheet("background:black")
    view.setAlignment(Qt.AlignLeft | Qt.AlignTop)
    dpr = app.primaryScreen().devicePixelRatio()

    def show(name):
        pm = QPixmap(os.path.join(ROOT, "tests", "samples", name))
        pm.setDevicePixelRatio(dpr)  # 1 image px = 1 physical px
        view.setPixmap(pm)

    show("manga.png")
    view.showFullScreen()
    overlay = Overlay((mon["left"], mon["top"], mon["width"], mon["height"]))
    engine = Engine(c, overlay, mon)
    engine.start()
    out_dir = os.path.join(ROOT, "tests", "out")
    os.makedirs(out_dir, exist_ok=True)
    t_start = time.perf_counter()
    plan = [("manga.png", None), ("game.png", None)]
    state = {"i": 0, "t_page": None, "stable_since": None, "last_n": -1}

    def snapshot(name):
        engine.suspended = True
        time.sleep(0.15)
        overlay.set_capture_excluded(False)
        app.processEvents()
        time.sleep(0.25)
        app.processEvents()
        with mss.MSS() as s:
            shot = s.grab(mon)
        overlay.set_capture_excluded(True)
        app.processEvents()
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        p = os.path.join(out_dir, "e2e_" + name)
        img.save(p)
        engine.suspended = False
        return p

    def tick():
        if not engine.ready.is_set():
            return
        if state["t_page"] is None:
            state["t_page"] = time.perf_counter()
        name = plan[state["i"]][0]
        n = len(engine.blocks)
        old = state.get("old_keys", set())
        done = n > 0 and not (set(engine.blocks) & old) and all(
            st.shown and (st.refined or not engine.ctx) for st in engine.blocks.values())
        if done and state["stable_since"] is None:
            state["stable_since"] = time.perf_counter()
            print(f"[e2e] {name}: {n} blocks translated {state['stable_since'] - state['t_page']:.2f} s "
                  f"after page shown (translator ready)")
        timeout = time.perf_counter() - state["t_page"] > 30
        if (state["stable_since"] and time.perf_counter() - state["stable_since"] > 1.0) or timeout:
            if timeout:
                print(f"[e2e] {name}: timeout, {n} blocks, shown={[st.shown for st in engine.blocks.values()]}")
            print("[e2e] screenshot:", snapshot(name))
            state["i"] += 1
            if state["i"] >= len(plan):
                engine.stop()
                app.quit()
                return
            state["old_keys"] = set(engine.blocks)
            show(plan[state["i"]][0])
            state.update(t_page=time.perf_counter(), stable_since=None)

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(50)
    QTimer.singleShot(240_000, app.quit)  # hard stop
    app.exec()
    print(f"[e2e] total {time.perf_counter() - t_start:.1f} s")


if __name__ == "__main__":
    main()
