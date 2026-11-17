"""Generate synthetic test screens: a manga page with vertical bubbles and a game dialog box."""
import os
import random

from PIL import Image, ImageDraw, ImageFont

OUT = os.path.join(os.path.dirname(__file__), "..", "tests", "samples")
W, H = 1920, 1200
FONTS = [r"C:\Windows\Fonts\yumin.ttf", r"C:\Windows\Fonts\msmincho.ttc",
         r"C:\Windows\Fonts\YuGothM.ttc", r"C:\Windows\Fonts\msgothic.ttc"]
# Real manga keeps ！？ upright in vertical text, so they are not remapped.
VERT = str.maketrans({"「": "﹁", "」": "﹂", "、": "︑", "。": "︒", "ー": "丨", "…": "︙",
                      "（": "︵", "）": "︶"})


def font(size):
    for f in FONTS:
        if os.path.exists(f):
            return ImageFont.truetype(f, size)
    raise SystemExit("no Japanese font found")


def vertical_bubble(d, cx, cy, columns, size):
    f = font(size)
    colw = int(size * 1.35)
    maxlen = max(len(c) for c in columns)
    bw = colw * len(columns) + size * 2
    bh = int(size * 1.08 * maxlen) + size * 2
    d.ellipse([cx - bw * 0.75, cy - bh * 0.62, cx + bw * 0.75, cy + bh * 0.62],
              fill="white", outline="black", width=3)
    x0 = cx + (colw * len(columns)) / 2 - colw  # first column on the right
    for ci, col in enumerate(columns):
        x = x0 - ci * colw
        y = cy - bh / 2 + size
        for ch in col.translate(VERT):
            d.text((x, y), ch, font=f, fill="black")
            y += size * 1.08


def manga():
    random.seed(1)
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    # panel borders + fake art (hatching) so detection is not trivially easy
    for box in [(20, 20, 940, 590), (960, 20, 1900, 590), (20, 610, 1900, 1180)]:
        d.rectangle(box, outline="black", width=5)
        for _ in range(120):
            x = random.randint(box[0], box[2]); y = random.randint(box[1], box[3])
            d.line([x, y, x + random.randint(-80, 80), y + random.randint(-80, 80)],
                   fill=(90, 90, 90), width=1)
    vertical_bubble(d, 300, 260, ["お前、いったい", "何者なんだ？"], 30)
    vertical_bubble(d, 720, 330, ["そんなこと、", "どうでもいいでしょ。"], 26)
    vertical_bubble(d, 1250, 270, ["今日はもう遅いから、", "また明日にしよう。"], 28)
    vertical_bubble(d, 1700, 300, ["待って！", "まだ話は", "終わってないよ！"], 30)
    vertical_bubble(d, 500, 890, ["ふふっ、やっぱり", "君は面白いね。"], 24)
    vertical_bubble(d, 1450, 880, ["この先には", "危険な魔物が", "いるらしい。"], 32)
    im.save(os.path.join(OUT, "manga.png"))


def game():
    im = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(im)
    for y in range(H):  # sky-ish gradient background
        d.line([0, y, W, y], fill=(80 + y // 12, 120 + y // 20, 200 - y // 10))
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    od.rounded_rectangle([120, 820, 1800, 1140], 24, fill=(10, 10, 30, 190))
    od.rounded_rectangle([140, 770, 460, 830], 12, fill=(60, 30, 80, 230))
    im = Image.alpha_composite(im.convert("RGBA"), ov).convert("RGB")
    d = ImageDraw.Draw(im)
    g = ImageFont.truetype(r"C:\Windows\Fonts\YuGothM.ttc", 34) if os.path.exists(
        r"C:\Windows\Fonts\YuGothM.ttc") else font(34)
    d.text((170, 778), "アリス", font=g, fill="white")
    lines = ["「ねえ、本当にこの道で合ってるの？",
             "　さっきから同じ場所を歩いてる気がするんだけど……」"]
    for i, t in enumerate(lines):
        d.text((180, 870 + i * 60), t, font=g, fill="white")
    small = ImageFont.truetype(r"C:\Windows\Fonts\YuGothM.ttc", 22) if os.path.exists(
        r"C:\Windows\Fonts\YuGothM.ttc") else font(22)
    x = 1100
    for t in ["セーブ", "ロード", "オート", "スキップ", "ログ", "設定"]:
        d.text((x, 1100), t, font=small, fill=(220, 220, 220))
        x += 110
    im.save(os.path.join(OUT, "game.png"))


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    manga()
    game()
    print("samples written to", os.path.abspath(OUT))
