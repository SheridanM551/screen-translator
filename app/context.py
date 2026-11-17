"""Translation context: rolling history of what was already translated, a user glossary,
and an auto-glossary of recurring proper nouns so names stay consistent.

Everything stored here is model-side Simplified Chinese.
"""
from __future__ import annotations

import json
import os
import re
import threading
from collections import Counter, deque

# katakana words of 3+ chars (names, places, items)
KATA = re.compile(r"[ァ-ヺ][ァ-ヺー・]{2,}")
# name + honorific: 美咲さん / 田中くん / アリス様 ...
HONOR = re.compile(r"([一-鿿々]{1,4}|[ァ-ヺー]{2,})(?=さん|くん|君|ちゃん|様|さま|先輩|先生|殿|たん)")
NAME_BLOCK = re.compile(r"^[一-鿿々ァ-ヺーぁ-ゖ・A-Za-z]{1,8}$")
KANA = re.compile(r"[぀-ヿ]")


def parse_glossary(text: str) -> dict[str, tuple[str, str]]:
    out = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or "->" not in line or line.startswith("//"):
            continue
        src, rest = line.split("->", 1)
        dst, _, note = rest.partition("#")
        if src.strip() and dst.strip():
            out[src.strip()] = (dst.strip(), note.strip())
    return out


class ContextManager:
    def __init__(self, store_path: str, user_glossary: str = "", history_lines: int = 12,
                 min_term_count: int = 2, history_chars: int = 400):
        self.path = store_path
        self.user = parse_glossary(user_glossary)
        self.auto: dict[str, str] = {}
        self.counts: Counter = Counter()
        self.pending: set[str] = set()
        self.hist: deque[tuple[str, str]] = deque(maxlen=history_lines)
        self.history_chars = history_chars
        self.min_term_count = min_term_count
        self.lock = threading.Lock()
        self._load()

    # ---------- persistence ----------
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.auto = dict(d.get("auto", {}))
            self.counts = Counter(d.get("counts", {}))
            for src, sc in d.get("history", []):
                self.hist.append((src, sc))
        except FileNotFoundError:
            pass
        except Exception:
            pass  # corrupt file → start fresh

    def save(self):
        with self.lock:
            d = {"auto": self.auto, "counts": dict(self.counts.most_common(500)),
                 "history": list(self.hist)}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def reset(self):
        with self.lock:
            self.auto.clear()
            self.counts.clear()
            self.pending.clear()
            self.hist.clear()
        self.save()

    # ---------- prompt material ----------
    def history(self, n: int | None = None) -> str:
        with self.lock:
            items = list(self.hist)
        if n is not None:
            items = items[-n:] if n > 0 else []
        out, size = [], 0
        for _, sc in reversed(items):  # newest first until the char budget is used
            if size + len(sc) > self.history_chars and out:
                break
            out.append(sc)
            size += len(sc)
        return "\n".join(reversed(out))

    def glossary_for(self, texts: list[str]) -> str:
        joined = "\n".join(texts)
        lines = []
        with self.lock:
            for src, (dst, note) in self.user.items():
                if src in joined:
                    lines.append(f"{src}->{dst}" + (f" #{note}" if note else ""))
            for src, dst in self.auto.items():
                if src in joined and src not in self.user:
                    lines.append(f"{src}->{dst}")
        return "\n".join(lines)

    # ---------- learning ----------
    def observe(self, texts: list[str], names: list[str] = ()) -> list[str]:
        """Count proper-noun candidates in newly seen text. Returns terms that just crossed the
        threshold and need a pinned translation (caller translates them and calls learn())."""
        found: Counter = Counter()
        for t in texts:
            for m in KATA.findall(t):
                found[m] += 1
            for m in HONOR.findall(t):
                found[m] += 1
        for n in names:
            found[n] += self.min_term_count  # a speaker name tag is a name for sure
        need = []
        with self.lock:
            for term, k in found.items():
                if term in self.user or term in self.auto or term in self.pending:
                    continue
                self.counts[term] += k
                if self.counts[term] >= self.min_term_count:
                    self.pending.add(term)
                    need.append(term)
        return need

    def learn(self, term: str, sc: str) -> bool:
        sc = sc.strip().strip("「」『』\"'。．.")
        ok = bool(sc) and "\n" not in sc and not KANA.search(sc) and len(sc) <= max(6, 3 * len(term))
        with self.lock:
            self.pending.discard(term)
            if ok:
                self.auto[term] = sc
        if ok:
            self.save()
        return ok

    def forget_pending(self, term: str):
        with self.lock:
            self.pending.discard(term)

    def commit(self, pairs: list[tuple[str, str]]):
        """Append translated blocks (reading order) to the history."""
        with self.lock:
            for src, sc in pairs:
                if len(src) < 3 or (self.hist and self.hist[-1][0] == src):
                    continue
                if any(h[0] == src for h in list(self.hist)[-4:]):
                    continue  # same line re-detected (e.g. after a scroll)
                self.hist.append((src, sc))


def is_name_block(text: str) -> bool:
    return bool(NAME_BLOCK.match(text))
