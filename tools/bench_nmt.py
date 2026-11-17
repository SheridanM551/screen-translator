"""Benchmark the non-LLM NMT backends (app/translator_nmt.py) for speed and quality.

Each model x device runs in its own subprocess so load time and memory are clean.

    python tools/bench_nmt.py                       # all models found, cpu + cuda
    python tools/bench_nmt.py --models opus,mt5l --devices cpu --threads 8
    python tools/bench_nmt.py --beams 1 --runs 10 --json bench.json

Models are expected under models/nmt/<dir> (see MODELS below for how each was built).
Memory: "RAM" is the process private-bytes growth caused by loading + warming up the
model; "VRAM" is the growth of total GPU memory used (nvidia-smi), so it includes the
CUDA context and is only meaningful when nothing else allocates on the GPU meanwhile.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
NMT_DIR = os.path.join(ROOT, "models", "nmt")

SENTENCES = [
    "お前、いったい何者なんだ？",
    "そんなこと、どうでもいいでしょ。",
    "今日はもう遅いから、また明日にしよう。",
    "待って！まだ話は終わってないよ！",
    "ふふっ、やっぱり君は面白いね。",
    "この先には危険な魔物がいるらしい。",
    "「ねえ、本当にこの道で合ってるの？さっきから同じ場所を歩いてる気がするんだけど……」",
    "セーブ",
    "べ、別にあんたのために作ったわけじゃないんだからね！",
    "くそっ…ここまでか…",
]

# key: (directory under models/nmt, kind, extra NmtTranslator kwargs, source)
MODELS = {
    "opus": ("opus-mt-ja-zh", "marian", {}, "shun89/opus-mt-ja-zh (converted, keep-pad fix)"),
    "mt5s": ("mt5-game-small", "mt5", {}, "iryneko571/mt5-translation-ja_zh-game-small"),
    "mt5c": ("mt5-small-ccmatrix", "mt5", {}, "iryneko571/mt5-small-translation-ja_zh"),
    "mt5l": ("mt5-game-large", "mt5", {}, "iryneko571/mt5-translation-ja_zh-game-large"),
    "nllb": ("nllb-600m", "nllb", {"nllb_target": "zho_Hant"}, "JustFrederik/nllb-200-distilled-600M-ct2-int8 -> zho_Hant"),
    "nllbS": ("nllb-600m", "nllb", {"nllb_target": "zho_Hans"}, "same, -> zho_Hans + s2twp"),
}


# ----------------------------------------------------------------- memory probes
def _private_bytes() -> int:
    """Private (commit) bytes of this process; Windows only, 0 elsewhere."""
    if sys.platform != "win32":
        return 0
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    k32 = ctypes.WinDLL("kernel32")
    psapi = ctypes.WinDLL("psapi")
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
    psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    return int(pmc.PrivateUsage)


def _gpu_used_mib() -> int | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return int(out.strip().splitlines()[0])
    except Exception:
        return None


# ------------------------------------------------------------------------ worker
def _ms(fn, runs: int) -> float:
    times = []
    for _ in range(runs):
        t = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t) * 1000)
    return statistics.median(times)


def worker(key: str, device: str, threads: int, beams: list[int], runs: int,
           compute_type: str | None) -> dict:
    sys.path.insert(0, ROOT)
    from app.translator_nmt import NmtTranslator

    d, kind, extra, _ = MODELS[key]
    import ctranslate2, opencc, sentencepiece  # noqa: F401,E401  (exclude import cost from RAM delta)

    ram0, gpu0 = _private_bytes(), (_gpu_used_mib() if device == "cuda" else None)
    t = time.perf_counter()
    tr = NmtTranslator(os.path.join(NMT_DIR, d), kind, device=device, threads=threads,
                       compute_type=compute_type, **extra)
    load_ms = (time.perf_counter() - t) * 1000
    tr.translate_batch(SENTENCES)  # warm-up (allocator, kernels)
    for s in SENTENCES:
        tr.translate_batch([s])
    ram1, gpu1 = _private_bytes(), (_gpu_used_mib() if device == "cuda" else None)

    res = {
        "model": key, "dir": d, "device": device, "compute_type": tr.compute_type,
        "threads": threads, "load_ms": load_ms,
        "ram_mib": (ram1 - ram0) / 2**20,
        "vram_mib": (gpu1 - gpu0) if gpu0 is not None and gpu1 is not None else None,
        "beams": {},
    }
    for b in beams:
        tr.beam_size = b
        per_sentence = [_ms(lambda s=s: tr.translate_batch([s]), runs) for s in SENTENCES]
        res["beams"][str(b)] = {
            "single_median_ms": statistics.median(per_sentence),
            "single_short_ms": per_sentence[0],
            "single_long_ms": per_sentence[6],
            "single_max_ms": max(per_sentence),
            "batch10_ms": _ms(lambda: tr.translate_batch(SENTENCES), runs),
            "outputs": tr.translate_batch(SENTENCES),
        }
    return res


# ------------------------------------------------------------------------ driver
def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default=",".join(MODELS), help=f"comma list of {list(MODELS)}")
    ap.add_argument("--devices", default="cpu,cuda")
    ap.add_argument("--threads", type=int, default=4, help="CPU intra-op threads")
    ap.add_argument("--beams", default="1,2,4")
    ap.add_argument("--runs", type=int, default=5, help="timed runs per measurement (median)")
    ap.add_argument("--compute-type", default=None, help="override CT2 compute type")
    ap.add_argument("--json", default=None, help="write raw results here")
    ap.add_argument("--worker", nargs=2, metavar=("MODEL", "DEVICE"), help=argparse.SUPPRESS)
    a = ap.parse_args()
    beams = [int(b) for b in a.beams.split(",")]

    if a.worker:
        r = worker(a.worker[0], a.worker[1], a.threads, beams, a.runs, a.compute_type)
        print("@@RESULT@@" + json.dumps(r, ensure_ascii=False))
        return

    results = []
    for key in a.models.split(","):
        d = MODELS[key][0]
        if not os.path.isfile(os.path.join(NMT_DIR, d, "model.bin")):
            print(f"skip {key}: models/nmt/{d}/model.bin not found", file=sys.stderr)
            continue
        for dev in a.devices.split(","):
            cmd = [sys.executable, os.path.abspath(__file__), "--worker", key, dev,
                   "--threads", str(a.threads), "--beams", a.beams, "--runs", str(a.runs)]
            if a.compute_type:
                cmd += ["--compute-type", a.compute_type]
            print(f"... {key} on {dev}", file=sys.stderr, flush=True)
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            line = next((ln for ln in p.stdout.splitlines() if ln.startswith("@@RESULT@@")), None)
            if line is None:
                err = (p.stderr or p.stdout).strip().splitlines()
                print(f"FAILED {key} on {dev}: {err[-1] if err else p.returncode}", file=sys.stderr)
                results.append({"model": key, "device": dev, "error": "\n".join(err[-5:])})
                continue
            results.append(json.loads(line[len("@@RESULT@@"):]))

    out = sys.stdout
    if hasattr(out, "reconfigure"):
        out.reconfigure(encoding="utf-8")
    b0 = str(beams[0])
    print(f"\n## Speed (beam={b0}, threads={a.threads}, median of {a.runs})\n")
    print("| model | device | compute | load ms | single ms (median / short / long) | 10-batch ms | RAM MiB | VRAM MiB |")
    print("|---|---|---|---|---|---|---|---|")
    for r in results:
        if "error" in r:
            print(f"| {r['model']} | {r['device']} | FAILED | | | | | |")
            continue
        s = r["beams"][b0]
        vram = f"{r['vram_mib']:.0f}" if r["vram_mib"] is not None else "-"
        print(f"| {r['model']} | {r['device']} | {r['compute_type']} | {r['load_ms']:.0f} | "
              f"{s['single_median_ms']:.1f} / {s['single_short_ms']:.1f} / {s['single_long_ms']:.1f} | "
              f"{s['batch10_ms']:.1f} | {r['ram_mib']:.0f} | {vram} |")

    if len(beams) > 1:
        print("\n## Beam cost (single median ms / 10-batch ms)\n")
        print("| model | device | " + " | ".join(f"beam {b}" for b in beams) + " |")
        print("|---|---|" + "---|" * len(beams))
        for r in results:
            if "error" in r:
                continue
            cells = [f"{r['beams'][str(b)]['single_median_ms']:.1f} / {r['beams'][str(b)]['batch10_ms']:.1f}" for b in beams]
            print(f"| {r['model']} | {r['device']} | " + " | ".join(cells) + " |")

    print("\n## Outputs\n")
    seen = set()
    for r in results:
        if "error" in r or r["model"] in seen:
            continue
        seen.add(r["model"])
        print(f"### {r['model']}  ({MODELS[r['model']][3]}, {r['device']}, beam {b0})")
        base = r["beams"][b0]["outputs"]
        for src, o in zip(SENTENCES, base):
            print(f"  {src}\n    -> {o}")
        for b in beams[1:]:
            diff = [(i, o) for i, o in enumerate(r["beams"][str(b)]["outputs"]) if o != base[i]]
            for i, o in diff:
                print(f"  [beam {b}] #{i + 1} -> {o}")
        print()

    for r in results:
        if "error" in r:
            print(f"FAILED {r['model']} on {r['device']}:\n{r['error']}\n")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
