# -*- coding: utf-8 -*-
"""生成手机访问用的二维码图片（含站点地址与说明）。

用法：
    python tools/make_qr.py [url] [out_png]

默认 url = https://chuanwudi46-ops.github.io/Big-A/
默认 out = docs/手机访问二维码.png
二维码下方附文字说明：用手机相机 / 微信扫一扫直接打开；也可长按识别。
"""
import sys
from pathlib import Path

import qrcode
from qrcode.constants import ERROR_CORRECT_H
from PIL import Image, ImageDraw, ImageFont

URL = sys.argv[1] if len(sys.argv) > 1 else "https://chuanwudi46-ops.github.io/Big-A/"
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent / "docs" / "手机访问二维码.png"

# 深色主题配色（与站点 #0e1116 一致），但保留高对比度保证扫描率
BG = (14, 17, 22)
CARD = (255, 255, 255)
FG = (14, 17, 22)
MUTED = (110, 118, 129)
ACCENT = (239, 68, 68)   # 站点红色强调

# ---- 1. 生成二维码本体（白底黑码，扫描兼容性最好）----
qr = qrcode.QRCode(version=None, error_correction=ERROR_CORRECT_H, box_size=12, border=3)
qr.add_data(URL)
qr.make(fit=True)
qr_img = qr.make_image(fill_color=FG, back_color=CARD).convert("RGB")

W = qr_img.width
PAD = 44

# ---- 2. 字体 ----
def _font(size, bold=False):
    cands = [
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ]
    for p in cands:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


f_title = _font(40, bold=True)
f_sub = _font(26)
f_url = _font(24)
f_dot = _font(28)

HEAD_H = 160
FOOT_H = 160

canvas = Image.new("RGB", (W + PAD * 2, HEAD_H + W + FOOT_H), BG)
d = ImageDraw.Draw(canvas)

# ---- 3. 顶部标题 ----
title = "板块评分工作台"
tw = d.textlength(title, font=f_title)
d.text(((canvas.width - tw) / 2, 34), title, font=f_title, fill=(255, 255, 255))
sub = "手机扫码直接打开 · 可添加到主屏幕"
sw_ = d.textlength(sub, font=f_sub)
d.text(((canvas.width - sw_) / 2, 84), sub, font=f_sub, fill=MUTED)


def _center(text, font, y, fill):
    w = d.textlength(text, font=font)
    d.text(((canvas.width - w) / 2, y), text, font=font, fill=fill)


# ---- 4. 二维码（带一圈白卡 + 圆角）----
card = Image.new("RGB", (W + 20, W + 20), CARD)
card.paste(qr_img, (10, 10))
canvas.paste(card, (PAD - 10, HEAD_H))

# ---- 5. 底部说明 ----
y = HEAD_H + W + 24
_center("用「相机」或「微信扫一扫」对准二维码", f_sub, y, (255, 255, 255))
y += 38
_center("微信内打开后 → 右上角菜单 → 用浏览器打开", f_sub, y, MUTED)
y += 38
_center(URL, f_url, y, ACCENT)

OUT.parent.mkdir(parents=True, exist_ok=True)
canvas.save(OUT)
print(f"saved -> {OUT}  ({canvas.width}x{canvas.height})")
