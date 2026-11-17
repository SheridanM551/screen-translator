"""OCR clean-up before translation.

Manga often draws the ellipsis as large / hollow dots, which OCR reads as 0, o, O, ○, 〇, °,
sometimes with a stray c or 8 at the end ("00o", "000.", "00c", "…0o"). Real numbers must
survive untouched ("20歳", "2025/12/31", "2.00", "1000円", "100点", "Book").
"""
from __future__ import annotations

import re

CIRC = "0OoＯ０〇○◯ｏ°º"
DOTS = "·・･.．。"
JP = r"぀-ヿ㐀-鿿ｦ-ﾟ「」『』（）、。！？!?～〜ー…‥"

# numbers / units / latin around a run mean it is real text, not a drawn ellipsis
_NUM_BEFORE = r"0-9０-９A-Za-z.,/:＄$¥￥#№"
_NUM_AFTER = r"0-9０-９A-Za-z.,/:%％円歳年月日時分秒人個回度点倍万億千百ｍ㎝㎏"

_DOT_RUN = re.compile(rf"[{re.escape(DOTS)}]{{2,}}")
# a run of 2+ circle-like glyphs, optionally ending in c/8, not glued to numbers or latin
_CIRC_RUN = re.compile(
    rf"(?<![{_NUM_BEFORE}])[{CIRC}]{{2,}}[c8]?(?![{_NUM_AFTER}])")
# junk glued to an existing ellipsis: "…0o", "…000.", "·…", "00…"
_AROUND_ELLIPSIS_AFTER = re.compile(
    rf"(?<=[…‥])[{CIRC}{re.escape(DOTS)}c]+(?![1-9１-９{_NUM_AFTER[3:]}])")
_AROUND_ELLIPSIS_BEFORE = re.compile(rf"(?<![{_NUM_BEFORE}])[{CIRC}{re.escape(DOTS)}]+(?=[…‥])")
_MANY = re.compile(r"[…‥]{3,}")
_JP_CHAR = re.compile(rf"[{JP}]")


def _touches_japanese(s: str, m: re.Match) -> bool:
    a, b = m.start(), m.end()
    left = s[a - 1] if a > 0 else ""
    right = s[b] if b < len(s) else ""
    # block start/end counts as a boundary (bubbles often start/end with an ellipsis)
    return (not left or not right or bool(_JP_CHAR.match(left)) or bool(_JP_CHAR.match(right))
            or left.isspace() or right.isspace())


def fix_ellipsis(s: str) -> str:
    if not s:
        return s
    s = _DOT_RUN.sub("…", s)
    s = _absorb(s)  # "…00o" is one ellipsis, not two
    if _JP_CHAR.search(s):  # only rewrite circle runs inside Japanese text
        s = _CIRC_RUN.sub(lambda m: "…" if _touches_japanese(s, m) else m.group(0), s)
        s = _absorb(s)
    return _MANY.sub("……", s)


def _absorb(s: str) -> str:
    s = _AROUND_ELLIPSIS_AFTER.sub("", s)
    return _AROUND_ELLIPSIS_BEFORE.sub("", s)
