"""One-shot quality check of the GGUF translation model via llama-completion (no server)."""
import os
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
from app.translator_llm import SYSTEM_PROMPT, build_user_prompt  # noqa: E402

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

if __name__ == "__main__":
    import opencc
    model = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        ROOT, "models", "Sakura-Galtransl-7B-v3.7-IQ4_XS.gguf")
    prompt = (f"<|im_start|>system\n{SYSTEM_PROMPT}<|im_end|>\n"
              f"<|im_start|>user\n{build_user_prompt(chr(10).join(SENTENCES))}<|im_end|>\n"
              f"<|im_start|>assistant\n")
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as f:
        f.write(prompt)
        pf = f.name
    exe = os.path.join(ROOT, "bin", "llama", "llama-completion.exe")
    r = subprocess.run([exe, "-m", model, "-ngl", "99", "-fa", "on", "-no-cnv", "-f", pf,
                        "-n", "400", "--temp", "0.3", "--top-p", "0.8", "--no-display-prompt",
                        "--simple-io"], capture_output=True)
    os.unlink(pf)
    out = r.stdout.decode("utf-8", "replace").replace("[end of text]", "").strip()
    cc = opencc.OpenCC("s2twp")
    lines = cc.convert(out).splitlines()
    for ja, zh in zip(SENTENCES, lines + [""] * (len(SENTENCES) - len(lines))):
        print(f"{ja}\n  -> {zh}")
    if len(lines) != len(SENTENCES):
        print(f"(line count mismatch: {len(lines)} vs {len(SENTENCES)})")
    tail = r.stderr.decode("utf-8", "replace").splitlines()
    print("\n".join(l for l in tail if "eval time" in l or "load time" in l))
