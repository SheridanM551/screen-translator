"""Translation via a local llama.cpp server running a Sakura / GalTransl model.

Speed strategy: one HTTP request per text block, issued concurrently; llama-server
batches the parallel slots on the GPU, so a whole page costs roughly as much as its
longest bubble. Results are cached by source text.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.request
from collections import OrderedDict

SYSTEM_PROMPT = (
    "你是一个视觉小说翻译模型，可以通顺地使用给定的术语表以指定的风格将日文翻译成简体中文，"
    "并联系上下文正确使用人称代词，注意不要混淆使役态和被动态的主语和宾语，"
    "不要擅自添加原文中没有的特殊符号，也不要擅自增加或减少换行。"
)


def build_user_prompt(text: str, history: str = "", glossary: str = "") -> str:
    p = ""
    if history:
        p += "历史翻译：" + history + "\n"
    p += "参考以下术语表（可为空，格式为src->dst #备注）：\n" + glossary + "\n"
    p += "根据以上术语表的对应关系和备注，结合历史剧情和上下文，将下面的文本从日文翻译成简体中文：\n"
    p += text
    return p


class LruCache:
    def __init__(self, cap: int = 4096):
        self.cap = cap
        self.d: OrderedDict[str, str] = OrderedDict()
        self.lock = threading.Lock()

    def get(self, k):
        with self.lock:
            v = self.d.get(k)
            if v is not None:
                self.d.move_to_end(k)
            return v

    def clear(self):
        with self.lock:
            self.d.clear()

    def put(self, k, v):
        with self.lock:
            self.d[k] = v
            self.d.move_to_end(k)
            while len(self.d) > self.cap:
                self.d.popitem(last=False)


_JOB = None


def _kill_with_parent(proc: subprocess.Popen) -> None:
    """Put the child in a Job Object that is killed when this process dies, so a crash
    never leaves an orphaned llama-server holding ~5 GB of VRAM."""
    global _JOB
    try:
        import ctypes as C
        from ctypes import wintypes as W

        k32 = C.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = W.HANDLE
        k32.OpenProcess.restype = W.HANDLE

        class BASIC(C.Structure):
            _fields_ = [("PerProcessUserTimeLimit", C.c_int64), ("PerJobUserTimeLimit", C.c_int64),
                        ("LimitFlags", W.DWORD), ("MinimumWorkingSetSize", C.c_size_t),
                        ("MaximumWorkingSetSize", C.c_size_t), ("ActiveProcessLimit", W.DWORD),
                        ("Affinity", C.c_size_t), ("PriorityClass", W.DWORD),
                        ("SchedulingClass", W.DWORD)]

        class IO(C.Structure):
            _fields_ = [(n, C.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class EXT(C.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", C.c_size_t),
                        ("JobMemoryLimit", C.c_size_t), ("PeakProcessMemoryUsed", C.c_size_t),
                        ("PeakJobMemoryUsed", C.c_size_t)]

        if _JOB is None:
            _JOB = k32.CreateJobObjectW(None, None)
            info = EXT()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            k32.SetInformationJobObject(_JOB, 9, C.byref(info), C.sizeof(info))
        h = k32.OpenProcess(0x0001 | 0x0100, False, proc.pid)  # TERMINATE | SET_QUOTA
        if h:
            k32.AssignProcessToJobObject(_JOB, h)
            k32.CloseHandle(h)
    except Exception:
        pass


class LlamaServer:
    """Starts llama-server as a child process (unless one already answers on the port)."""

    def __init__(self, exe: str, model: str, port: int = 8765, ctx: int = 8192,
                 parallel: int = 8, ngl: int = 99, extra: list[str] | None = None):
        self.exe, self.model, self.port = exe, model, port
        self.args = [exe, "-m", model, "-ngl", str(ngl), "-c", str(ctx), "-np", str(parallel),
                     "-fa", "on", "--host", "127.0.0.1", "--port", str(port), "--no-webui"]
        self.args += extra or []
        self.proc: subprocess.Popen | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def healthy(self) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/health", timeout=0.5) as r:
                return r.status == 200
        except Exception:
            return False

    def start(self, timeout: float = 120.0, log_path: str | None = None) -> None:
        if self.healthy():
            return
        if not os.path.exists(self.exe) or not os.path.exists(self.model):
            raise FileNotFoundError("llama-server or model missing; run setup.ps1")
        log = open(log_path, "ab") if log_path else subprocess.DEVNULL
        self.proc = subprocess.Popen(self.args, stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        _kill_with_parent(self.proc)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server exited with code {self.proc.returncode}")
            if self.healthy():
                return
            time.sleep(0.25)
        raise TimeoutError("llama-server did not become healthy")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class LlmTranslator:
    """All model I/O is Simplified Chinese (what Sakura/GalTransl is trained on); history and
    glossary are kept in SC too. display() converts to the configured output script."""

    supports_context = True

    def __init__(self, base_url: str, to_traditional: str | None = "s2twp",
                 temperature: float = 0.3, top_p: float = 0.8, max_tokens: int = 256):
        self.url = base_url.rstrip("/") + "/v1/chat/completions"
        self.temperature, self.top_p, self.max_tokens = temperature, top_p, max_tokens
        self.cache = LruCache()  # source text -> SC translation
        self._cc = None
        if to_traditional:
            import opencc
            self._cc = opencc.OpenCC(to_traditional)

    def _post(self, text: str, history: str, glossary: str, max_tokens: int) -> str:
        body = {
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(text, history, glossary)},
            ],
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": max_tokens,
            "stream": False,
            "cache_prompt": True,
        }
        req = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:
            out = json.loads(r.read())
        return out["choices"][0]["message"]["content"].strip()

    def display(self, sc: str) -> str:
        return self._cc.convert(sc) if self._cc is not None else sc

    def peek(self, text: str) -> str | None:
        """Cached translation, already converted for display."""
        sc = self.cache.get(text)
        return None if sc is None else self.display(sc)

    def raw(self, text: str, history: str = "", glossary: str = "", max_tokens: int | None = None) -> str:
        """Uncached single request; returns SC."""
        return self._post(text, history, glossary, max_tokens or self.max_tokens)

    def translate(self, text: str, history: str = "", glossary: str = "", overwrite: bool = True) -> str:
        """One block; returns SC. overwrite=False keeps an existing (e.g. refined) cache entry."""
        sc = self._post(text, history, glossary, self.max_tokens)
        if overwrite or self.cache.get(text) is None:
            self.cache.put(text, sc)
        return sc

    def translate_lines(self, lines: list[str], history: str = "", glossary: str = "") -> list[str] | None:
        """Several blocks in one request (one per line, reading order) so the model sees the whole
        page. Returns SC per line, or None if the model changed the line count."""
        clean = [l.replace("\n", " ") for l in lines]
        budget = 64 + sum(3 * len(l) + 8 for l in clean)
        out = self._post("\n".join(clean), history, glossary, budget)
        res = [l.strip() for l in out.split("\n") if l.strip()]
        if len(res) != len(clean):
            return None
        for src, sc in zip(clean, res):
            self.cache.put(src, sc)
        return res
