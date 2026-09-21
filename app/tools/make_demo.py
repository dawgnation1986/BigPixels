#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
生成首页用的示例图（合成，不是真实照片）。

放大算法最怕三类输入：细线条、小字号、噪点。这三张就是照着这三件事做的，
尺寸刻意压到 360×240，4 倍出来刚好 1440×960 —— 一眼能看出差别。

    python app/tools/make_demo.py
输出：web/demo/{lineart,text,grain}.png
"""
from __future__ import annotations

import math
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.core.paths import WEB_DIR                                      # noqa: E402

OUT = os.path.join(WEB_DIR, "demo")      # 生成的示例图是前端资源，放 web/ 下
W, H = 360, 240
SS = 4                      # 超采样倍数，先画大再缩，线条才有干净的抗锯齿

FONTS = [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc",
         r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\arial.ttf"]


def font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONTS:
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def down(im: Image.Image) -> Image.Image:
    return im.resize((W, H), Image.LANCZOS)


# ------------------------------ 1. 线稿 ------------------------------ #
def lineart() -> Image.Image:
    im = Image.new("L", (W * SS, H * SS), 255)
    d = ImageDraw.Draw(im)
    S = SS
    ink = 0

    # 同心圆弧（越往外越细，考验线条重建）
    for i in range(26):
        r = (14 + i * 3.6) * S
        d.arc([int(60 * S - r), int(96 * S - r), int(60 * S + r), int(96 * S + r)],
              200, 340, fill=ink, width=max(1, S - i // 6))

    # 平行排线（考验会不会糊成一片灰）
    for i in range(30):
        x = (176 + i * 1.9) * S
        d.line([(x, 22 * S), (x + 26 * S, 116 * S)], fill=ink, width=max(1, S // 2 if i % 3 else S))

    # 细网格
    for i in range(24):
        d.line([(12 * S, (126 + i * 1.5) * S), (348 * S, (126 + i * 1.5) * S)],
               fill=ink, width=1)
    for i in range(60):
        d.line([((12 + i * 5.6) * S, 126 * S), ((12 + i * 5.6) * S, 162 * S)],
               fill=ink, width=1)

    # 波浪线
    pts = [((10 + i * 0.7) * S, (196 + 9 * math.sin(i * 0.22) + 4 * math.sin(i * 0.71)) * S)
           for i in range(int(W / 0.7) - 12)]
    d.line(pts, fill=ink, width=max(1, S - 1))

    # 小字 + 几何
    d.text((14 * S, 172 * S), "細 線 0.5px / 排線 / 摩爾紋", fill=ink, font=font(7 * S))
    d.rectangle([300 * S, 30 * S, 344 * S, 74 * S], outline=ink, width=S)
    for i in range(0, 44, 2):
        d.line([(300 * S + i * S, 74 * S), (344 * S, 74 * S - i * S)], fill=ink, width=1)

    return down(im).convert("RGB")


# ------------------------------ 2. 小字 ------------------------------ #
def text() -> Image.Image:
    im = Image.new("L", (W * SS, H * SS), 250)
    d = ImageDraw.Draw(im)
    S = SS
    ink = 28

    d.text((14 * S, 12 * S), "放大前：这张图只有 360 × 240", fill=ink, font=font(11 * S))
    d.line([(14 * S, 28 * S), (346 * S, 28 * S)], fill=ink, width=1)

    body = [
        "深度学习超分辨率靠的是卷积网络",
        "从大量高清/低清图对里学到的先验。",
        "它不是把像素插值放大，而是猜出",
        "原本应该存在的那些边缘和纹理。",
        "",
        "The quick brown fox jumps over 0123456789",
        "waifu2x-cunet · Real-ESRGAN x4plus · SwinIR",
    ]
    y = 36
    for ln in body:
        d.text((14 * S, y * S), ln, fill=ink, font=font(8.5 * S if ln and ln[0].isascii() else 9 * S))
        y += 13

    y += 4
    for ln in ["小字号正文 6pt — 屏幕截图里最常见的那种糊",
               "带下划线与字重的混排 ABCdef 中文"]:
        d.text((14 * S, y * S), ln, fill=ink, font=font(6.5 * S))
        y += 11

    d.text((268 * S, 196 * S), "1440×960", fill=ink, font=font(9 * S))
    return down(im).convert("RGB")


# ------------------------------ 3. 噪点 ------------------------------ #
def grain() -> Image.Image:
    y = np.linspace(0, 1, H, dtype=np.float32)[:, None]
    sky = np.zeros((H, W, 3), np.float32)
    sky[..., 0] = 0.06 + 0.30 * y            # R
    sky[..., 1] = 0.10 + 0.42 * y            # G
    sky[..., 2] = 0.20 + 0.55 * y            # B

    # 远处山脊
    rng = np.random.default_rng(7)
    ridge = 0.52 + 0.07 * np.sin(np.linspace(0, 7, W)) + 0.04 * np.sin(np.linspace(0, 23, W))
    ridge = np.cumsum(rng.normal(0, 0.012, W)).clip(-0.08, 0.08) + ridge
    for x in range(W):
        top = int(ridge[x] * H)
        sky[top:, x] *= np.array([0.36, 0.40, 0.44], np.float32)

    # 水面横纹
    for r in range(int(0.72 * H), H):
        k = 1.0 + 0.10 * math.sin(r * 0.9)
        sky[r, :, :] *= k

    im = Image.fromarray((sky.clip(0, 1) * 255).astype(np.uint8))
    a = np.asarray(im, np.float32) / 255.0
    a += rng.normal(0, 0.085, a.shape).astype(np.float32)      # 强高斯噪声
    a = np.clip(a, 0, 1)
    return Image.fromarray((a * 255 + 0.5).astype(np.uint8))


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, fn in (("lineart", lineart), ("text", text), ("grain", grain)):
        p = os.path.join(OUT, f"{name}.png")
        fn().save(p)
        print(f"  + demo/{name}.png  {os.path.getsize(p) / 1024:.1f} KB")


if __name__ == "__main__":
    main()
