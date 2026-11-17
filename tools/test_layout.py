"""OCR + grouping on the sample screens; prints blocks and timings."""
import os
import sys
import time

import numpy as np
from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from app.layout import group_lines, sample_background  # noqa: E402
from app.ocr_oneocr import OneOcr  # noqa: E402

if __name__ == "__main__":
    ocr = OneOcr(os.path.join(ROOT, "ocr"))
    for f in ("manga.png", "game.png"):
        img = np.ascontiguousarray(np.asarray(Image.open(os.path.join(ROOT, "tests", "samples", f))
                                              .convert("RGBA"))[:, :, [2, 1, 0, 3]])
        h, w = img.shape[:2]
        ocr.recognize_bgra(img, w, h)
        t0 = time.perf_counter()
        lines = ocr.recognize_bgra(img, w, h)
        t1 = time.perf_counter()
        blocks = group_lines(lines)
        t2 = time.perf_counter()
        print(f"\n=== {f}: ocr {1000 * (t1 - t0):.0f} ms, group {1000 * (t2 - t1):.1f} ms, "
              f"{len(lines)} lines -> {len(blocks)} blocks")
        for b in blocks:
            print(f"  {'V' if b.vertical else 'H'} {b.rect} size={b.char_size:.0f} "
                  f"bg={sample_background(img, b.rect)} conf={b.conf:.2f} {b.text}")
