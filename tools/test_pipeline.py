"""Headless test of Engine bookkeeping (no GUI, no server): OCR real sample frames,
fake translators + fake overlay; checks add/keep/remove/cache/invalidate and the
context logic (fast pass → page refine, history order, glossary learning)."""
import os
import sys
import tempfile
import threading
import time
import types
from dataclasses import dataclass

import numpy as np
from PIL import Image

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

try:
    import app.overlay  # noqa: F401
except Exception:  # overlay not built yet → stub just RenderBlock
    m = types.ModuleType("app.overlay")

    @dataclass
    class RenderBlock:
        id: str
        rect: tuple
        text: str
        vertical: bool
        bg: tuple
        fg: tuple = None
        char_size: float = 0.0

    m.RenderBlock = RenderBlock
    sys.modules["app.overlay"] = m

from app import config as cfgmod  # noqa: E402
from app.context import ContextManager  # noqa: E402
from app.main import Engine  # noqa: E402
from app.ocr_oneocr import OneOcr  # noqa: E402
from app.translator_llm import LruCache  # noqa: E402


class FakeOverlay:
    def __init__(self):
        self.shown = {}
        self.history = []  # every text ever pushed, per id
        self.lock = threading.Lock()

    def upsert_blocks(self, bs):
        with self.lock:
            for b in bs:
                self.shown[b.id] = b
                self.history.append((b.id, b.text))

    def remove_blocks(self, ids):
        with self.lock:
            for i in ids:
                self.shown.pop(i, None)

    def clear(self):
        with self.lock:
            self.shown.clear()

    def set_hidden(self, h):
        pass


class FakeTr:
    """No context support (like the NMT backend)."""
    supports_context = False

    def __init__(self):
        self.cache = LruCache()
        self.calls = 0

    def display(self, s):
        return s

    def peek(self, t):
        return self.cache.get(t)

    def translate(self, t, history="", glossary="", overwrite=True):
        time.sleep(0.05)
        self.calls += 1
        self.cache.put(t, "ZH:" + t)
        return "ZH:" + t


class FakeCtxTr(FakeTr):
    supports_context = True

    def __init__(self):
        super().__init__()
        self.single = []   # (text, history, glossary)
        self.pages = []    # (lines, history, glossary)

    def translate(self, t, history="", glossary="", overwrite=True):
        time.sleep(0.05)
        self.calls += 1
        self.single.append((t, history, glossary))
        if overwrite or self.cache.get(t) is None:
            self.cache.put(t, "F:" + t)
        return "F:" + t

    def translate_lines(self, lines, history="", glossary=""):
        time.sleep(0.3)
        self.pages.append((lines, history, glossary))
        out = ["R:" + l for l in lines]
        for l, o in zip(lines, out):
            self.cache.put(l, o)
        return out

    def raw(self, text, history="", glossary="", max_tokens=None):
        return {"アリス": "爱丽丝"}.get(text, "某某")


def load(name):
    a = np.asarray(Image.open(os.path.join(ROOT, "tests", "samples", name)).convert("RGBA"))
    return np.ascontiguousarray(a[:, :, [2, 1, 0, 3]])


def wait_for(pred, timeout=3):
    t = time.time()
    while time.time() - t < timeout:
        if pred():
            return True
        time.sleep(0.02)
    return False


ok = True


def check(label, cond):
    global ok
    ok &= bool(cond)
    print(("PASS " if cond else "FAIL ") + label)


def basic(ocr, manga, game):
    print("--- basic bookkeeping (context-free engine)")
    c = cfgmod.load()
    ov = FakeOverlay()
    eng = Engine(c, ov, {"left": 0, "top": 0, "width": 1920, "height": 1200})
    eng.translator = FakeTr()
    eng.ready.set()
    eng._process(ocr, manga, 0, 0, forced=False)
    check("manga: 6 blocks translated + shown", wait_for(lambda: len(ov.shown) == 6) and eng.translator.calls == 6)
    eng._process(ocr, manga, 0, 0, forced=False)
    time.sleep(0.2)
    check("same frame again: nothing re-translated", eng.translator.calls == 6 and len(ov.shown) == 6)
    eng._process(ocr, game, 0, 0, forced=False)
    check("page change: manga blocks removed, game blocks shown",
          wait_for(lambda: len(ov.shown) == 4) and not any("お前" in k for k in ov.shown))
    calls = eng.translator.calls
    eng._process(ocr, manga, 0, 0, forced=False)
    time.sleep(0.05)
    check("back to manga: served from cache instantly", len(ov.shown) == 6 and eng.translator.calls == calls)
    eng.prev_texts = set()
    eng.blocks.clear(); ov.clear()
    eng._process(ocr, game, 0, 0, forced=True)
    time.sleep(0.1)
    check("forced pass with unstable text: nothing shown", len(ov.shown) == 0)
    eng._process(ocr, game, 0, 0, forced=True)
    check("forced pass, text stable across 2 passes: shown", wait_for(lambda: len(ov.shown) == 4))
    S = 8
    changed = np.zeros((1200 // S, 1920 // S), bool)
    changed[860 // S:970 // S, 190 // S:1050 // S] = True
    eng._invalidate(changed, S, 0, 0, 0.25)
    check("changed area hides its translation immediately",
          len(ov.shown) == 3 and not any("ねえ" in k for k in ov.shown))
    eng.blocks.clear(); ov.clear(); eng.prev_texts = set()
    eng._process(ocr, manga[100:700, 200:1000].copy(), 200, 100, forced=False)
    wait_for(lambda: len(ov.shown) == 2)
    rects = [b.rect for b in ov.shown.values()]
    check(f"region offset applied to rects {rects}", rects and all(r[0] >= 200 and r[1] >= 100 for r in rects))
    eng.pool.shutdown(wait=True)
    eng.coord.shutdown(wait=True)


def contextual(ocr, manga, game):
    print("--- context: fast pass → page refine, history, glossary")
    c = cfgmod.load()
    ov = FakeOverlay()
    eng = Engine(c, ov, {"left": 0, "top": 0, "width": 1920, "height": 1200})
    tr = eng.translator = FakeCtxTr()
    store = os.path.join(tempfile.mkdtemp(), "ctx.json")
    eng.ctx = ContextManager(store, "魔物->魔兽 #用户术语", history_lines=12, min_term_count=2)
    eng.ready.set()

    eng._process(ocr, manga, 0, 0, forced=False)
    done = wait_for(lambda: len(ov.shown) == 6 and all(b.text.startswith("R:") for b in ov.shown.values()))
    fast_first = all(any(t.startswith("F:") for i, t in ov.history if i == k) for k in ov.shown)
    check("manga page: fast pass shown first, then replaced by page refine", done and fast_first)
    lines, _, gloss = tr.pages[-1]
    order = [l[:4] for l in lines]
    check(f"refine got the page in manga reading order {order}",
          order[:4] == ["待って!", "今日はも", "そんなこ", "お前、い"] and set(order[4:]) == {"この先に", "ふふっ、"}
          and order[4] == "この先に")
    check("user glossary entry passed when its term is on the page", "魔物->魔兽" in gloss)
    hist = eng.ctx.history()
    check("refined lines committed to history", hist.count("R:") == 6)

    eng._process(ocr, game, 0, 0, forced=False)
    wait_for(lambda: len(ov.shown) == 4 and all(b.text.startswith("R:") for b in ov.shown.values()))
    check("speaker name tag learned into auto glossary", eng.ctx.auto.get("アリス") == "爱丽丝")
    lines, h2, gloss2 = tr.pages[-1]
    check("game page refine used full history + learned name", "アリス->爱丽丝" in gloss2 and h2.count("R:") >= 6)

    # a single new line (typical game dialogue advance) → one request with full context
    eng.prev_texts = set(); eng.blocks.clear(); ov.clear()
    tr.cache.clear()  # otherwise the line is served from cache (it was on the previous page)
    single_before = len(tr.single)
    crop = game[840:1000, 150:1100].copy()
    eng._process(ocr, crop, 150, 840, forced=False)
    wait_for(lambda: len(tr.single) > single_before, 2)
    time.sleep(0.4)  # give a (wrong) refine pass the chance to show up
    t, h, g = tr.single[-1] if len(tr.single) > single_before else ("", "", "")
    check(f"single new block: translated once with full history, no refine "
          f"(calls {len(tr.single) - single_before}, history lines {h.count('R:')}, pages {len(tr.pages)})",
          len(tr.single) == single_before + 1 and h.count("R:") >= 6 and len(tr.pages) == 2)
    eng.ctx.save()
    eng2 = ContextManager(store)
    check("history + auto glossary persisted to disk", eng2.auto.get("アリス") == "爱丽丝" and len(eng2.hist) >= 6)
    eng.pool.shutdown(wait=True)
    eng.coord.shutdown(wait=True)


if __name__ == "__main__":
    ocr = OneOcr(os.path.join(ROOT, "ocr"))
    manga, game = load("manga.png"), load("game.png")
    basic(ocr, manga, game)
    contextual(ocr, manga, game)
    print("ALL PASS" if ok else "SOME FAILED")
