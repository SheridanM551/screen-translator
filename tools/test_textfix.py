"""Cases taken from real OCR output (logs/app.log) + numbers that must survive."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.textfix import fix_ellipsis  # noqa: E402

FIX = [
    ("00oでもよかつたちよっつっとだけどれた·00", "…でもよかつたちよっつっとだけどれた…"),
    ("やばいきた…00o", "やばいきた…"),
    ("やばいきた…000.", "やばいきた…"),
    ("やべえ…緊張する000やばいやばい…o", "やべえ…緊張する…やばいやばい…"),
    ("00○足曲げたらなんとか入れるんかな?", "…足曲げたらなんとか入れるんかな?"),
    ("00cてか洗濯物どうしよつ脱いだやつ回したほうがええんがなっ?", "…てか洗濯物どうしよつ脱いだやつ回したほうがええんがなっ?"),
    ("008タオルあるしかさばるなあ2持てる?大丈夫?", "…タオルあるしかさばるなあ2持てる?大丈夫?"),
    ("000o洗濯回すのあとでええかなここれ?", "…洗濯回すのあとでええかなここれ?"),
    ("えっと~給湯温度が…0o", "えっと~給湯温度が…"),
    ("00oつかさ22人で湯船入ったら絶対狭いよなあ笑", "…つかさ22人で湯船入ったら絶対狭いよなあ笑"),
    ("・・・そうか", "…そうか"),
    ("そうか...", "そうか…"),
    ("なに。。。", "なに…"),
    ("〇〇〇まあいいか", "…まあいいか"),
    ("ふう……", "ふう……"),
]
KEEP = [
    "20歳の大学生", "おっしゃ~ほんならいくかあ2.00", "40Cくらい", "発行日:2025/12/31",
    "O Rino @-my二日", "1000円です", "100点満点", "Book Zoo cool", "ふふっ", "ゾーン", "No.1だ",
    "今日は2000年", "200人いる", "0時に集合", "残り00:30", "3.00倍", "お前、いったい何者なんだ?",
    "よいしよ2000",  # ambiguous (digit-glued) → left alone on purpose
]

if __name__ == "__main__":
    bad = 0
    for src, want in FIX:
        got = fix_ellipsis(src)
        ok = got == want
        bad += not ok
        print(("PASS " if ok else "FAIL ") + f"{src}  ->  {got}" + ("" if ok else f"   (want {want})"))
    for src in KEEP:
        got = fix_ellipsis(src)
        ok = got == src
        bad += not ok
        print(("PASS " if ok else "FAIL ") + f"keep {src}" + ("" if ok else f"  ->  {got}"))
    print("ALL PASS" if not bad else f"{bad} FAILED")
