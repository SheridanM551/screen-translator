"""config.json loading/saving with defaults (user file only needs the keys it overrides)."""
from __future__ import annotations

import copy
import json
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_PATH = os.path.join(ROOT, "config.json")

DEFAULTS: dict = {
    "monitor": 1,                 # mss index: 1 = primary monitor
    "region": None,               # null = whole monitor, or [x0, y0, x1, y1] physical px
    "engine": "llm",              # "llm" (Sakura/GalTransl via llama.cpp) or "nmt"
    "output": "s2twp",            # OpenCC config: s2twp = Traditional (Taiwan phrasing), s2t, or null = Simplified
    "llm": {
        "server": "bin/llama/llama-server.exe",
        "model": "models/Sakura-Galtransl-7B-v3.7-IQ4_XS.gguf",
        "port": 8765,
        "parallel": 10,           # concurrent requests (fast pass of a page + refine + term lookups)
        "ctx": 16384,             # total KV cache shared by the slots (~0.9 GB VRAM)
        "gpu_layers": 99,
        "temperature": 0.3,
        "top_p": 0.8,
        "glossary": "",           # lines like  アリス->艾莉絲 #女主角  (or put them in glossary.txt)
    },
    "context": {
        "enabled": True,
        "mode": "refine",         # refine: fast per-block pass, then whole-page pass replaces it
                                  # batch:  whole-page pass only (slower first text, no flicker)
                                  # single: one request per block, full history (no page context)
        "history_lines": 12,      # recent translated lines given to the model as story context
        "history_chars": 400,     # ...capped to this many characters
        "fast_history_lines": 3,  # short history for the fast pass (keeps it fast)
        "min_term_count": 2,      # a name/term seen this often gets a pinned translation
        "store": "data/context.json",  # history + auto glossary survive restarts
    },
    "nmt": {                      # non-LLM fallback: fast but clearly worse than the LLM
        "model_dir": "models/nmt/mt5-game-large",  # best of the tested NMT models (~0.3 s/line CPU)
        "kind": "mt5",            # or models/nmt/opus-mt-ja-zh + "marian" (~20 ms/line, rough quality)
        "device": "cpu",          # cpu keeps the GPU free; "cuda" ~2x faster, +1.5 GB VRAM
        "threads": 4,
    },
    "detect": {
        "poll_ms": 60,            # change-detection interval
        "settle_ms": 100,         # wait until the screen is still this long before OCR
        "max_wait_ms": 600,       # ...but OCR at least this often while it keeps changing (animations)
        "diff_threshold": 24,     # per-pixel change (0-255) that counts as "changed"
        "invalidate_ratio": 0.25, # hide a translation at once if this share of its area changed
        "min_conf": 0.5,
        "require_kana": True,     # skip text without kana (Chinese/English UI, art noise)
    },
    "render": {
        "pad": 4,
    },
    "ui": {
        "pin_toolbar": False,     # keep the floating toolbar open instead of auto-hiding
    },
    "hotkeys": {
        "toggle": "ctrl+alt+t",   # translation on/off
        "peek": "ctrl+alt+h",     # hide/show overlay to see the original
        "region": "ctrl+alt+r",   # drag-select a capture region (saved)
        "fullscreen": "ctrl+alt+f",  # back to whole monitor (saved)
        "reset_context": "ctrl+alt+c",  # new game/manga: forget history, auto glossary, cache
        "quit": "ctrl+alt+q",
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict:
    user = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
    return _merge(DEFAULTS, user)


def save_keys(**kv) -> None:
    """Update only the given top-level keys in config.json, keeping the user's other edits."""
    user = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
    for k, v in kv.items():  # dict values are merged, so other sub-keys the user set survive
        user[k] = _merge(user[k], v) if isinstance(v, dict) and isinstance(user.get(k), dict) else v
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(user, f, ensure_ascii=False, indent=2)


def path(p: str) -> str:
    return p if os.path.isabs(p) else os.path.join(ROOT, p)
