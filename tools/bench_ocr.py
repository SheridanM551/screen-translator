"""Benchmark OneOCR speed/accuracy on sample screens and a live desktop grab."""
import os
import sys
import time

import numpy as np
from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from app.ocr_oneocr import OneOcr  # noqa: E402


def to_bgra(img: Image.Image) -> np.ndarray:
    a = np.asarray(img.convert("RGBA"))
    return np.ascontiguousarray(a[:, :, [2, 1, 0, 3]])


def bench(ocr, name, arr, runs=10):
    h, w = arr.shape[:2]
    lines = ocr.recognize_bgra(arr, w, h)  # warm-up
    t = time.perf_counter()
    for _ in range(runs):
        lines = ocr.recognize_bgra(arr, w, h)
    ms = (time.perf_counter() - t) / runs * 1000
    print(f"\n=== {name} {w}x{h}: {ms:.0f} ms/frame, {len(lines)} lines")
    for ln in lines:
        x0, y0, x1, y1 = ln.rect
        ori = "V" if (y1 - y0) > 1.3 * (x1 - x0) and len(ln.text) > 1 else "H"
        print(f"  [{ori}] ({x0:4.0f},{y0:4.0f},{x1:4.0f},{y1:4.0f}) c={ln.conf:.2f} {ln.text}")


if __name__ == "__main__":
    t = time.perf_counter()
    ocr = OneOcr(os.path.join(ROOT, "ocr"))
    print(f"init {1000 * (time.perf_counter() - t):.0f} ms")
    sd = os.path.join(ROOT, "tests", "samples")
    for f in ("manga.png", "game.png"):
        p = os.path.join(sd, f)
        if os.path.exists(p):
            bench(ocr, f, to_bgra(Image.open(p)))
    import mss
    with mss.mss() as s:
        mon = s.monitors[1]
        t = time.perf_counter()
        shot = s.grab(mon)
        grab_ms = (time.perf_counter() - t) * 1000
        arr = np.frombuffer(shot.raw, dtype=np.uint8).reshape(shot.height, shot.width, 4)
        print(f"\nmss grab {shot.width}x{shot.height}: {grab_ms:.0f} ms")
        bench(ocr, "desktop", np.ascontiguousarray(arr), runs=5)
