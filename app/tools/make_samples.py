# -*- coding: utf-8 -*-
"""
生成用于对比测试的图样：
  samples/gt_art.png    卡通/插画 高清原图（640x480，硬边缘 + 平涂色块 + 细线）
  samples/gt_photo.png  照片感 高清原图（640x480，渐变 + 纹理 + 噪点）
  samples/lr_art.png    上面的 1/4 缩略图（160x120）—— 当作“低分原图”喂给模型
  samples/lr_photo.png
  samples/lr_photo_noisy.png  额外加噪的版本，用来试降噪档位
"""
import os
import numpy as np
from PIL import Image, ImageDraw

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.core.paths import ROOT, SAMPLE_DIR                             # noqa: E402

OUT = SAMPLE_DIR
os.makedirs(OUT, exist_ok=True)
W, H = 640, 480


def gradient(w, h, c0, c1, vertical=True):
    t = np.linspace(0, 1, h if vertical else w, dtype=np.float32)
    if not vertical:
        t = t[None, :]
    else:
        t = t[:, None]
    arr = np.zeros((h, w, 3), np.float32)
    for c in range(3):
        arr[:, :, c] = c0[c] + (c1[c] - c0[c]) * t
    return arr


def make_art():
    """卡通/插画：平涂色块 + 硬边缘描边 + 细密排线"""
    img = Image.new("RGB", (W, H), (250, 248, 244))
    d = ImageDraw.Draw(img)
    pal = [(232, 205, 176), (246, 224, 200), (94, 122, 156), (58, 74, 102),
           (226, 149, 130), (247, 214, 205), (120, 148, 122), (206, 226, 208)]

    # 背景几何块（大色块，检验色块边缘是否干净）
    d.polygon([(0, 300), (160, 240), (330, 320), (520, 250), (640, 310), (640, 480), (0, 480)],
              fill=pal[6], outline=(40, 52, 44), width=2)
    d.polygon([(0, 360), (180, 320), (400, 400), (640, 350), (640, 480), (0, 480)],
              fill=pal[7], outline=(40, 52, 44), width=2)
    for i in range(5):
        x = 60 + i * 120
        d.polygon([(x, 250), (x + 46, 200), (x + 92, 250)], fill=pal[4], outline=(120, 52, 44), width=2)

    # 人物：头发 + 脸 + 眼睛 + 嘴
    cx = 320
    d.ellipse([cx - 96, 96, cx + 96, 296], fill=pal[2], outline=(30, 38, 56), width=3)
    d.polygon([(cx - 104, 196), (cx - 84, 96), (cx - 20, 62), (cx + 40, 66),
               (cx + 96, 108), (cx + 108, 200), (cx + 62, 150), (cx - 60, 156)],
              fill=pal[3], outline=(24, 30, 46), width=3)
    d.ellipse([cx - 62, 168, cx - 26, 216], fill=(252, 252, 252), outline=(30, 38, 56), width=2)
    d.ellipse([cx + 26, 168, cx + 62, 216], fill=(252, 252, 252), outline=(30, 38, 56), width=2)
    d.ellipse([cx - 50, 180, cx - 36, 204], fill=(46, 86, 132))
    d.ellipse([cx + 38, 180, cx + 52, 204], fill=(46, 86, 132))
    d.ellipse([cx - 48, 186, cx - 42, 194], fill=(255, 255, 255))
    d.ellipse([cx + 40, 186, cx + 46, 194], fill=(255, 255, 255))
    d.arc([cx - 24, 218, cx + 24, 248], 20, 160, fill=(150, 72, 68), width=2)
    d.arc([cx - 70, 196, cx - 24, 236], 200, 340, fill=(30, 38, 56), width=2)
    d.arc([cx + 24, 196, cx + 70, 236], 200, 340, fill=(30, 38, 56), width=2)

    # 细密排线：考验模型能不能把细线续上
    for i in range(70):
        y = 330 + i * 2
        d.line([(i * 9, y), (i * 9 + 26, y + 6)], fill=(70, 88, 68), width=1)
    # 小字块（AI 一定会乱编的地方，用来看极限）。字母是随便挑的测试图案，
    # 不是品牌名 —— 故意用无意义的组合，好看出模型把哪儿补错了。
    # 改这几个字会换掉 samples/ 里那两张基准图，README 上的实测数据就得重跑，别顺手改。
    for i, ch in enumerate("BIGJPG"):
        x = 430 + i * 26
        d.rectangle([x, 60, x + 16, 76], outline=(60, 60, 60), width=1)
        d.line([(x + 4, 68), (x + 12, 68)], fill=(60, 60, 60), width=2)
    # 细网格
    for i in range(0, W, 16):
        d.line([(i, 0), (i, 44)], fill=(180, 180, 176), width=1)
    for j in range(0, 44, 8):
        d.line([(0, j), (W, j)], fill=(180, 180, 176), width=1)
    return img


def make_photo():
    """照片感：渐变天空 + 山体 + 水面波纹 + 细草 + 噪点"""
    arr = gradient(W, H, (0.16, 0.36, 0.62), (0.92, 0.78, 0.60))
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    sun = np.stack([
        np.exp(-(((xx - 470) / 90.0) ** 2 + ((yy - 90) / 90.0) ** 2)) * 1.0,
        np.exp(-(((xx - 470) / 110.0) ** 2 + ((yy - 90) / 110.0) ** 2)) * 0.85,
        np.exp(-(((xx - 470) / 140.0) ** 2 + ((yy - 90) / 140.0) ** 2)) * 0.55,
    ], axis=2)
    arr = np.clip(arr + sun * 0.55, 0, 1)

    img = Image.fromarray((arr * 255).astype(np.uint8))
    d = ImageDraw.Draw(img)
    # 远山
    d.polygon([(0, 250), (90, 175), (180, 250), (280, 150), (400, 250), (520, 190), (640, 250),
               (640, 320), (0, 320)], fill=(92, 104, 118))
    d.polygon([(0, 285), (120, 225), (250, 290), (390, 215), (520, 285), (640, 240),
               (640, 340), (0, 340)], fill=(58, 70, 84))
    # 水面
    d.rectangle([0, 320, W, H], fill=(46, 62, 82))
    for i in range(60):
        y = 322 + i * 2.6
        off = int(6 * np.sin(i * 0.7))
        d.line([(0, y), (W, y)], fill=(96, 122, 150) if i % 3 else (140, 168, 195), width=1)
        d.line([(off, y), (off + 120, y)], fill=(178, 198, 216), width=1)
    # 细草：高频细节，最容易看出插值和 AI 的差距
    rng = np.random.default_rng(7)
    for _ in range(1400):
        x = float(rng.uniform(0, W))
        y = float(rng.uniform(316, H))
        ln = float(rng.uniform(1.5, 5.0))
        g = int(rng.uniform(70, 150))
        d.line([(x, y), (x + rng.uniform(-1.2, 1.2), y - ln)], fill=(g // 3, g, g // 2), width=1)
    out = np.asarray(img, np.float32) / 255.0
    out = np.clip(out + rng.normal(0, 0.012, out.shape).astype(np.float32), 0, 1)
    return Image.fromarray((out * 255 + 0.5).astype(np.uint8))


def main():
    art, photo = make_art(), make_photo()
    art.save(os.path.join(OUT, "gt_art.png"))
    photo.save(os.path.join(OUT, "gt_photo.png"))
    for name, im in (("art", art), ("photo", photo)):
        lr = im.resize((W // 4, H // 4), Image.LANCZOS)
        lr.save(os.path.join(OUT, f"lr_{name}.png"))
        print(f"  lr_{name}.png  {lr.size[0]}x{lr.size[1]}  <- gt_{name}.png {W}x{H}")
    # 加噪版低分图，用来对比降噪档位
    a = np.asarray(Image.open(os.path.join(OUT, "lr_photo.png")), np.float32) / 255.0
    rng = np.random.default_rng(11)
    n = np.clip(a + rng.normal(0, 0.06, a.shape).astype(np.float32), 0, 1)
    Image.fromarray((n * 255 + 0.5).astype(np.uint8)).save(os.path.join(OUT, "lr_photo_noisy.png"))
    print("  lr_photo_noisy.png  <- 给低分照片加了 sigma=0.06 的噪声")


if __name__ == "__main__":
    main()
