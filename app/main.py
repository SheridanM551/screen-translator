"""Real-time screen translator: capture → change detection → OneOCR → grouping →
translation (parallel, cached, context-aware) → click-through overlay invisible to capture.

Translation of a new page (several new blocks):
  fast pass   – every block in parallel with a short history → shown within ~1 s
  refine pass – the whole page in reading order + full history + glossary in one request,
                run concurrently; replaces the fast text on screen where it differs.
A single new block (typical game line) is translated once with the full context.

Run:  .venv\\Scripts\\python.exe -m app.main
"""
from __future__ import annotations

import logging
import os
import re
import signal
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait as fwait
from dataclasses import dataclass

import mss
import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from . import config as cfgmod
from .context import ContextManager, is_name_block
from .layout import TextBlock, group_lines, reading_order, sample_background
from .ocr_oneocr import OneOcr
from .textfix import fix_ellipsis

log = logging.getLogger("rt")

_JP_SPACE = re.compile(r"(?<=[^\x00-\x7f])\s+(?=[^\x00-\x7f])")


def normalize(text: str) -> str:
    return fix_ellipsis(_JP_SPACE.sub("", text).strip())


@dataclass
class BlockState:
    key: str
    block: TextBlock
    text: str            # normalized source text
    rect: tuple          # monitor-relative physical px
    bg: tuple
    sc: str | None = None    # translation currently shown (model-side text)
    refined: bool = False    # page-level refine result already applied
    shown: bool = False


_SPEAKER_INLINE = re.compile(r"^[【\[]?([^「『【】\[\]\s]{1,8})[】\]]?[「『]")


class NmtAdapter:
    """Gives the NMT backend the same interface as LlmTranslator (no context support)."""

    supports_context = False

    def __init__(self, c: dict):
        from .translator_llm import LruCache
        from .translator_nmt import NmtTranslator
        n = c["nmt"]
        self.impl = NmtTranslator(cfgmod.path(n["model_dir"]), n["kind"], n.get("device", "cpu"),
                                  n.get("threads", 4))
        self.cache = LruCache()

    def display(self, s):
        return s

    def peek(self, text):
        return self.cache.get(text)

    def translate(self, text, history="", glossary="", overwrite=True):
        hit = self.cache.get(text)
        if hit is None:
            hit = self.impl.translate_batch([text])[0]
            self.cache.put(text, hit)
        return hit

    def translate_lines(self, lines, history="", glossary=""):
        res = self.impl.translate_batch(list(lines))  # one batched decode: ~2.5x faster per page
        for src, zh in zip(lines, res):
            self.cache.put(src, zh)
        return res


class Engine:
    def __init__(self, c: dict, overlay, monitor: dict):
        self.c = c
        self.overlay = overlay
        self.monitor = monitor
        self.region = tuple(c["region"]) if c.get("region") else None
        self.enabled = True
        self.suspended = False  # pause capture but keep the overlay as is (tests / screenshots)
        self.error: str | None = None   # shown in the control panel
        self.stats = {"ocr_ms": 0.0, "tr_ms": 0.0, "page_ms": 0.0}
        self.recent: deque = deque(maxlen=40)  # (source, shown translation) for the panel
        self.blocks: dict[str, BlockState] = {}
        self.lock = threading.Lock()
        self.prev_texts: set[str] = set()
        self.stop_evt = threading.Event()
        self.wake = threading.Event()
        self.ready = threading.Event()
        self.reset_evt = threading.Event()
        self.server = None
        self.translator = None
        self.ctx: ContextManager | None = None
        par = c["llm"]["parallel"] if c["engine"] == "llm" else 2
        self.pool = ThreadPoolExecutor(max_workers=par, thread_name_prefix="tr")
        # page coordinators wait on term lookups in self.pool → separate pool (no deadlock)
        self.coord = ThreadPoolExecutor(max_workers=2, thread_name_prefix="page")

    # ---------- lifecycle ----------
    def start(self):
        threading.Thread(target=self._init_translator, daemon=True, name="init").start()
        threading.Thread(target=self._loop, daemon=True, name="capture").start()

    def stop(self):
        self.stop_evt.set()
        self.wake.set()
        self.coord.shutdown(wait=False, cancel_futures=True)
        self.pool.shutdown(wait=False, cancel_futures=True)
        if self.ctx:
            try:
                self.ctx.save()
            except Exception:
                pass
        if self.server:
            self.server.stop()

    def switch_engine(self, kind: str):
        """Change translation engine at runtime ("llm" / "nmt")."""
        if kind == self.c["engine"] and (self.ready.is_set() or self.error is None):
            return
        self.ready.clear()
        old_server, self.server = self.server, None
        self.translator, self.ctx, self.error = None, None, None
        self.c["engine"] = kind
        self._drop_all()
        if old_server:
            old_server.stop()
        threading.Thread(target=self._init_translator, daemon=True, name="init").start()

    def _init_translator(self):
        c = self.c
        t0 = time.perf_counter()
        self.error = None
        try:
            if c["engine"] == "llm":
                from .translator_llm import LlamaServer, LlmTranslator
                l = c["llm"]
                self.server = LlamaServer(cfgmod.path(l["server"]), cfgmod.path(l["model"]),
                                          l["port"], l["ctx"], l["parallel"], l["gpu_layers"])
                os.makedirs(cfgmod.path("logs"), exist_ok=True)
                self.server.start(log_path=cfgmod.path("logs/llama-server.log"))
                self.translator = LlmTranslator(self.server.base, c["output"], l["temperature"],
                                                l["top_p"])
                cx = c["context"]
                if cx["enabled"]:
                    user_gloss = l.get("glossary", "")
                    gpath = cfgmod.path("glossary.txt")
                    if os.path.exists(gpath):
                        with open(gpath, encoding="utf-8") as f:
                            user_gloss += "\n" + f.read()
                    self.ctx = ContextManager(cfgmod.path(cx["store"]), user_gloss,
                                              cx["history_lines"], cx["min_term_count"],
                                              cx["history_chars"])
            else:
                self.translator = NmtAdapter(c)
        except Exception as e:
            log.exception("translator failed to start")
            self.error = f"翻譯引擎啟動失敗：{e}"
            return
        log.info("translator ready (%s) in %.1f s", c["engine"], time.perf_counter() - t0)
        self.ready.set()

    # ---------- user actions (any thread) ----------
    def set_enabled(self, on: bool):
        self.enabled = on
        if not on:
            self._drop_all()
        self.wake.set()
        log.info("translation %s", "ON" if on else "OFF")

    def set_region(self, region):
        self.region = tuple(region) if region else None
        self._drop_all()
        self.reset_evt.set()
        self.wake.set()
        log.info("region = %s", self.region or "full monitor")

    def _drop_all(self):
        with self.lock:
            self.blocks.clear()
            self.prev_texts = set()
        self.overlay.clear()

    # ---------- capture loop ----------
    def _loop(self):
        d = self.c["detect"]
        poll, settle, max_wait = d["poll_ms"] / 1000, d["settle_ms"] / 1000, d["max_wait_ms"] / 1000
        thr, inv_ratio, S = d["diff_threshold"], d["invalidate_ratio"], 8
        try:
            ocr = OneOcr(cfgmod.path("ocr"))
        except Exception:
            log.exception("OCR init failed")
            return
        mon = self.monitor
        with mss.MSS() as sct:
            prev = None
            dirty, first, last = True, time.monotonic(), time.monotonic()
            while not self.stop_evt.is_set():
                t_loop = time.monotonic()
                if not self.enabled or self.suspended:
                    prev = None
                    self.wake.wait(0.25)
                    self.wake.clear()
                    continue
                if self.reset_evt.is_set():
                    self.reset_evt.clear()
                    prev = None
                rx0, ry0, rx1, ry1 = self.region or (0, 0, mon["width"], mon["height"])
                shot = sct.grab({"left": mon["left"] + rx0, "top": mon["top"] + ry0,
                                 "width": rx1 - rx0, "height": ry1 - ry0})
                frame = np.frombuffer(shot.raw, np.uint8).reshape(shot.height, shot.width, 4)
                small = frame[::S, ::S, 1].astype(np.int16)
                now = time.monotonic()
                if prev is not None and prev.shape == small.shape:
                    changed = np.abs(small - prev) > thr
                    if changed.any():
                        if not dirty:
                            first = now
                        dirty, last = True, now
                        self._invalidate(changed, S, rx0, ry0, inv_ratio)
                else:
                    dirty, first, last = True, now, now - settle  # new region → OCR right away
                prev = small
                if dirty and (now - last >= settle or now - first >= max_wait):
                    dirty = False
                    try:
                        self._process(ocr, frame, rx0, ry0, forced=(now - last < settle))
                    except Exception:
                        log.exception("process failed")
                elapsed = time.monotonic() - t_loop
                if elapsed < poll:
                    self.wake.wait(poll - elapsed)
                    self.wake.clear()

    def _invalidate(self, changed: np.ndarray, S: int, rx0: int, ry0: int, ratio: float):
        gone = []
        with self.lock:
            for k, st in list(self.blocks.items()):
                x0, y0, x1, y1 = st.rect
                sx0, sy0 = max(0, (x0 - rx0) // S), max(0, (y0 - ry0) // S)
                sx1, sy1 = max(sx0 + 1, (x1 - rx0) // S + 1), max(sy0 + 1, (y1 - ry0) // S + 1)
                sub = changed[sy0:sy1, sx0:sx1]
                if sub.size and sub.mean() > ratio:
                    del self.blocks[k]
                    gone.append(k)
        if gone:
            self.overlay.remove_blocks(gone)

    def _process(self, ocr: OneOcr, frame: np.ndarray, rx0: int, ry0: int, forced: bool):
        d = self.c["detect"]
        t0 = time.perf_counter()
        h, w = frame.shape[:2]
        lines = ocr.recognize_bgra(frame, w, h)
        t1 = time.perf_counter()
        self.stats["ocr_ms"] = (t1 - t0) * 1000
        blocks = group_lines(lines, d["min_conf"], d["require_kana"])
        texts_now = set()
        keep, new, instant, gone = set(), [], [], []
        pad = self.c["render"]["pad"]
        with self.lock:
            for b in blocks:
                text = normalize(b.text)
                texts_now.add(text)
                if forced and text not in self.prev_texts:
                    continue  # still changing (typewriter effect etc.) — wait until stable
                rect = (b.rect[0] + rx0, b.rect[1] + ry0, b.rect[2] + rx0, b.rect[3] + ry0)
                match = self._match(text, rect, b.char_size)
                if match:
                    keep.add(match)
                    continue
                key = f"{text}@{rect[0]},{rect[1]}"
                st = BlockState(key, b, text, rect, sample_background(frame, b.rect, pad))
                self.blocks[key] = st
                keep.add(key)
                new.append(st)
            for k in list(self.blocks):
                if k not in keep:
                    del self.blocks[k]
                    gone.append(k)
            self.prev_texts = texts_now
        if gone:
            self.overlay.remove_blocks(gone)
        tr = self.translator
        todo = []
        for st in new:
            hit = tr.peek(st.text) if tr else None
            if hit is not None:
                st.sc = tr.cache.get(st.text)
                instant.append(self._render(st, hit))
            else:
                todo.append(st)
        if instant:
            self.overlay.upsert_blocks(instant)
        if todo:
            ordered = reading_order(todo, lambda s: s.rect, lambda s: s.block.vertical)
            self.coord.submit(self._page_job, ordered, time.perf_counter())
        if new or gone:
            log.info("ocr %3.0f ms | %d lines -> %d blocks | new %d (cached %d) | removed %d%s",
                     (t1 - t0) * 1000, len(lines), len(blocks), len(new), len(instant), len(gone),
                     " | forced" if forced else "")

    def _match(self, text, rect, size):
        tol = max(6.0, size * 0.6)
        for k, st in self.blocks.items():
            if st.text == text and all(abs(a - b) <= tol for a, b in zip(st.rect, rect)):
                return k
        return None

    def _render(self, st: BlockState, zh: str):
        from .overlay import RenderBlock
        st.shown = True
        return RenderBlock(id=st.key, rect=st.rect, text=zh, vertical=st.block.vertical,
                           bg=st.bg, fg=None, char_size=st.block.char_size)

    def _note(self, pairs, t_submit: float, tag: str = ""):
        """Record for the control panel: recent translations + latency since detection."""
        self.stats["tr_ms"] = (time.perf_counter() - t_submit) * 1000
        for src, shown in pairs:
            self.recent.append((src, shown, tag))

    def _alive(self, states):
        with self.lock:
            return [s for s in states if self.blocks.get(s.key) is s]

    @staticmethod
    def _is_speaker(st: BlockState, others) -> bool:
        """Short horizontal label sitting just above/left of a bigger text block (game name box)."""
        if st.block.vertical or not is_name_block(st.text):
            return False
        x0, y0, x1, y1 = st.rect
        s = st.block.char_size
        for o in others:
            if o is st or len(o.text) <= len(st.text):
                continue
            ox0, oy0, _, _ = o.rect
            if 0 <= oy0 - y1 <= 2.5 * s and abs(ox0 - x0) <= 4 * s:
                return True
        return False

    # ---------- translation jobs ----------
    def _page_job(self, states: list[BlockState], t_submit: float):
        """Coordinator for one batch of new blocks (runs in self.coord)."""
        if not self.ready.wait(timeout=300):
            return
        alive = self._alive(states)
        if not alive:
            return
        ctx, cc = self.ctx, self.c["context"]
        if ctx is None or not getattr(self.translator, "supports_context", False):
            if len(alive) > 1 and hasattr(self.translator, "translate_lines"):
                self._plain_batch(alive, t_submit)
            else:
                for st in alive:
                    self.pool.submit(self._block_job, st, "", "", True, t_submit)
            return
        texts = [s.text for s in alive]
        names = [s.text for s in alive if self._is_speaker(s, alive)]
        names += [m.group(1) for t in texts if (m := _SPEAKER_INLINE.match(t))]
        terms = ctx.observe(texts, names)
        term_futs = [self.pool.submit(self._term_job, t) for t in terms]
        mode = cc["mode"]
        if len(alive) == 1 or mode == "single":
            fwait(term_futs, timeout=2)  # so this first translation already uses the new names
            for st in alive:
                self.pool.submit(self._block_job, st, ctx.history(), ctx.glossary_for([st.text]),
                                 True, t_submit)
            return
        if mode == "refine":  # fast pass, shown as soon as each block is done
            short = ctx.history(cc["fast_history_lines"])
            for st in alive:
                self.pool.submit(self._block_job, st, short, ctx.glossary_for([st.text]),
                                 False, t_submit)
        fwait(term_futs, timeout=2)
        self._refine(alive, t_submit)

    def _block_job(self, st: BlockState, history: str, glossary: str, commit: bool, t_submit: float):
        if not self._alive([st]):
            return  # text left the screen before we got to it
        tr = self.translator
        t0 = time.perf_counter()
        try:
            sc = tr.translate(st.text, history, glossary, overwrite=commit)
        except Exception as e:
            log.warning("translate failed: %s", e)
            return
        with self.lock:
            if self.blocks.get(st.key) is not st or st.refined:
                return
            st.sc = sc
            rb = self._render(st, tr.display(sc))
        self.overlay.upsert_blocks([rb])
        self._note([(st.text, rb.text)], t_submit, "" if commit else "快")
        if commit and self.ctx:
            self.ctx.commit([(st.text, sc)])
            self.ctx.save()
        log.info("%s %4.0f ms (since detected %4.0f ms)", "translated" if commit else "fast pass ",
                 (time.perf_counter() - t0) * 1000, (time.perf_counter() - t_submit) * 1000)
        log.debug("%s => %s", st.text, sc)

    def _plain_batch(self, states: list[BlockState], t_submit: float):
        """Context-free engines (NMT): translate the whole page in one batched call."""
        tr = self.translator
        t0 = time.perf_counter()
        try:
            res = tr.translate_lines([s.text for s in states])
        except Exception as e:
            log.warning("batch translate failed: %s", e)
            return
        updates = []
        with self.lock:
            for st, zh in zip(states, res):
                if self.blocks.get(st.key) is st:
                    st.sc = zh
                    updates.append(self._render(st, tr.display(zh)))
        if updates:
            self.overlay.upsert_blocks(updates)
            self._note([(st.text, rb.text) for st, rb in zip(states, updates)], t_submit)
        log.info("translated %d blocks (batch) in %4.0f ms (since detected %4.0f ms)", len(states),
                 (time.perf_counter() - t0) * 1000, (time.perf_counter() - t_submit) * 1000)

    def _refine(self, states: list[BlockState], t_submit: float):
        alive = self._alive(states)
        if not alive:
            return
        ctx, tr = self.ctx, self.translator
        texts = [s.text for s in alive]
        t0 = time.perf_counter()
        try:
            res = tr.translate_lines(texts, ctx.history(), ctx.glossary_for(texts))
        except Exception as e:
            log.warning("refine failed: %s", e)
            res = None
        if res is None:
            log.info("refine skipped (model changed line count / error); keeping fast results")
            ctx.commit([(s.text, s.sc) for s in alive if s.sc])
            ctx.save()
            return
        ctx.commit(list(zip(texts, res)))
        ctx.save()
        updates, changed, noted = [], 0, []
        with self.lock:
            for st, sc in zip(alive, res):
                if self.blocks.get(st.key) is not st:
                    continue
                st.refined = True
                if st.sc != sc:
                    changed += st.sc is not None
                    st.sc = sc
                    updates.append(self._render(st, tr.display(sc)))
                    noted.append((st.text, updates[-1].text))
        if updates:
            self.overlay.upsert_blocks(updates)
            self._note(noted, t_submit, "準")
        log.info("refined %d blocks with page context in %4.0f ms (since detected %4.0f ms), "
                 "%d adjusted", len(alive), (time.perf_counter() - t0) * 1000,
                 (time.perf_counter() - t_submit) * 1000, changed)
        for t, sc in zip(texts, res):
            log.debug("[refine] %s => %s", t, sc)

    def _term_job(self, term: str):
        try:
            sc = self.translator.raw(term, max_tokens=16)
        except Exception:
            self.ctx.forget_pending(term)
            return
        ok = self.ctx.learn(term, sc)
        log.info("glossary: %s new term", "learned" if ok else "rejected")
        log.debug("[term] %s -> %s (%s)", term, sc, "ok" if ok else "rejected")

    def reset_context(self):
        if self.ctx:
            self.ctx.reset()
        if self.translator is not None:
            self.translator.cache.clear()
        self._drop_all()
        log.info("context, glossary and cache cleared")


class _Bridge(QObject):
    action = Signal(str)


def setup_logging():
    os.makedirs(cfgmod.path("logs"), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fh = logging.FileHandler(cfgmod.path("logs/app.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)  # source text + translation go to the file only
    root.addHandler(fh)
    if sys.stdout is not None:  # pythonw has no console
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        ch.setLevel(logging.INFO)   # console stays free of Japanese text (it would be OCR'd too)
        root.addHandler(ch)


def _already_running() -> bool:
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    _already_running.handle = k32.CreateMutexW(None, False, "Local\\rt-jp-zh-screen-translator")
    return ctypes.get_last_error() == 183  # ERROR_ALREADY_EXISTS


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["llm", "nmt"], help="override config engine for this run")
    ap.add_argument("--exit-after", type=float, default=0, help="quit after N seconds (smoke tests)")
    ap.add_argument("--panel-png", help="save a picture of the control panel to this path (tests)")
    args, _ = ap.parse_known_args()
    setup_logging()
    c = cfgmod.load()
    if args.engine:
        c["engine"] = args.engine
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationDisplayName("即時日翻中")

    if _already_running():
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(None, "即時日翻中", "程式已經在執行了。\n"
                                "工具列在螢幕最上方中央（滑鼠移到最上面），或點右下角系統匣的「譯」圖示。")
        return

    from .control_panel import ControlPanel
    from .hotkeys import GlobalHotkeys
    from .overlay import Overlay
    from .region_highlight import RegionHighlight
    from .region_picker import pick_region
    from .toolbar import FloatingBar

    with mss.MSS() as s:
        mon = dict(s.monitors[c["monitor"]])
    mon_rect = (mon["left"], mon["top"], mon["width"], mon["height"])
    overlay = Overlay(mon_rect)  # shows itself
    highlight = RegionHighlight(mon_rect)
    engine = Engine(c, overlay, mon)

    hidden = {"v": False}
    picking = {"v": False}
    bridge = _Bridge()

    def on_action(name: str):
        if name == "toggle":
            engine.set_enabled(not engine.enabled)
        elif name == "peek":
            hidden["v"] = not hidden["v"]
            overlay.set_hidden(hidden["v"])
        elif name == "region":
            if picking["v"]:
                return
            picking["v"] = True
            was_on = engine.enabled
            engine.set_enabled(False)
            overlay.set_hidden(True)
            bar.begin_pick()

            def done(r):
                picking["v"] = False
                bar.end_pick()
                overlay.set_hidden(hidden["v"])
                if r == "full" or (r and r[0] <= 0 and r[1] <= 0
                                   and r[2] >= mon["width"] and r[3] >= mon["height"]):
                    cfgmod.save_keys(region=None)
                    engine.set_region(None)
                elif r and r[2] - r[0] >= 32 and r[3] - r[1] >= 32:
                    cfgmod.save_keys(region=list(r))
                    engine.set_region(r)
                engine.set_enabled(was_on)
                if r is not None:
                    highlight.show_region(engine.region)  # confirm the new range briefly
                    QTimer.singleShot(1200, highlight.hide_region)

            pick_region(done, mon_rect, current_region=engine.region)
        elif name == "reset_context":
            engine.reset_context()
        elif name == "fullscreen":
            cfgmod.save_keys(region=None)
            engine.set_region(None)
        elif name == "quit":
            panel.quit_now()
            app.quit()

    bridge.action.connect(on_action)
    hk = GlobalHotkeys()
    hk_errors = []
    for name, combo in c["hotkeys"].items():
        try:
            hk.register(combo, lambda n=name: bridge.action.emit(n))
        except Exception as e:
            hk_errors.append(combo)
            log.warning("hotkey %s (%s) unavailable: %s", name, combo, e)

    panel = ControlPanel(engine, on_action, c["hotkeys"], cfgmod.save_keys, hk_errors)
    bar = FloatingBar(engine, on_action, c["hotkeys"], lambda: hidden["v"], highlight,
                      panel.show_panel, cfgmod.save_keys, pinned=c["ui"]["pin_toolbar"])
    bar.start(show_for_ms=3000)
    if hk_errors:
        panel.show_panel()  # make sure the user sees which hotkeys are unavailable

    signal.signal(signal.SIGINT, lambda *_: on_action("quit"))
    tick = QTimer()
    tick.start(200)
    tick.timeout.connect(lambda: None)  # let Python see Ctrl+C

    engine.start()
    if args.panel_png:  # tests: panel + expanded toolbar pictures
        def snap():
            panel.grab().save(args.panel_png)
            bar.grab().save(args.panel_png.replace(".png", "_toolbar.png"))
        QTimer.singleShot(1500, snap)
    if args.exit_after:
        QTimer.singleShot(int(args.exit_after * 1000), lambda: on_action("quit"))
    hk_desc = ", ".join(f"{k}={v}" for k, v in c["hotkeys"].items())
    log.info("running on monitor %s %dx%d, region=%s | hotkeys: %s", c["monitor"], mon["width"],
             mon["height"], c.get("region") or "full", hk_desc)
    try:
        app.exec()
    finally:
        engine.stop()
        try:
            hk.stop()
        except Exception:
            pass
        log.info("bye")


if __name__ == "__main__":
    main()
