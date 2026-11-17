"""Check (offline, via llama-completion — no server) that the model honours the glossary,
keeps line count for a whole-page request with history, and gives short term translations."""
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from app.translator_llm import SYSTEM_PROMPT, build_user_prompt  # noqa: E402

MODEL = os.path.join(ROOT, "models", "Sakura-Galtransl-7B-v3.7-IQ4_XS.gguf")
EXE = os.path.join(ROOT, "bin", "llama", "llama-completion.exe")


def run(text, history="", glossary="", n=200):
    prompt = (f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n"
              f"<|im_start|>user\n{build_user_prompt(text, history, glossary)}<|im_end|>\n"
              f"<|im_start|>assistant\n")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as f:
        f.write(prompt)
    t = time.perf_counter()
    r = subprocess.run([EXE, "-m", MODEL, "-ngl", "99", "-fa", "on", "-no-cnv", "-f", f.name,
                        "-n", str(n), "--temp", "0.3", "--top-p", "0.8", "--no-display-prompt",
                        "--simple-io"], capture_output=True)
    os.unlink(f.name)
    return r.stdout.decode("utf-8", "replace").replace("[end of text]", "").strip(), time.perf_counter() - t


if __name__ == "__main__":
    print("== 1. glossary is honoured (deliberately unusual rendering)")
    out, _ = run("アリスは小さく笑った。\nアリス「ユウくん、また明日ね」", glossary="アリス->亚莉丝\nユウ->小悠")
    print(out)
    print("   ->", "PASS" if "亚莉丝" in out and "小悠" in out else "FAIL")

    print("== 2. without glossary (baseline)")
    out, _ = run("アリスは小さく笑った。\nアリス「ユウくん、また明日ね」")
    print(out)

    print("== 3. whole page + history keeps line count")
    page = ["待って！まだ話は終わってないよ！", "今日はもう遅いから、また明日にしよう。",
            "そんなこと、どうでもいいでしょ。", "お前、いったい何者なんだ？",
            "この先には危険な魔物がいるらしい。", "ふふっ、やっぱり君は面白いね。"]
    hist = "亚莉丝「我才不会输给你呢！」\n少年握紧了剑。"
    out, dt = run("\n".join(page), history=hist, glossary="魔物->魔兽")
    lines = [l for l in out.split("\n") if l.strip()]
    print(out)
    print(f"   -> {len(lines)}/{len(page)} lines", "PASS" if len(lines) == len(page) else "FAIL",
          "| glossary 魔兽:", "PASS" if "魔兽" in out else "FAIL")

    print("== 4. term lookups are short")
    for term in ["アリス", "ユウ", "エクスカリバー"]:
        out, _ = run(term, n=16)
        print(f"   {term} -> {out}")
