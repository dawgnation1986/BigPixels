# -*- coding: utf-8 -*-
"""
效果评测：拿 make_samples.py 生成的高清原图当标尺，
把「双三次插值」和「AI 超分各预设」放在同一把尺子下比 PSNR / SSIM，并出对比图。

用法：
    python bench.py                          # 跑默认矩阵
    python bench.py --scale 4
"""
from __future__ import annotations

import argparse
import os
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import sys

# 直接跑这个文件时项目根不在 sys.path 上，得自己挂上去
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app.core as U                            # noqa: E402
from app.core.metrics import psnr, ssim         # noqa: E402  指标只此一份，离线和网页端共用
SAMP = os.path.join(ROOT, "samples")
OUT = os.path.join(ROOT, "outputs", "bench")


# ----------------------------- 拼图 ----------------------------- #
def _font(size: int):
    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf",
              r"C:\Windows\Fonts\arial.ttf"):
        if os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def strip(items, crop, zoom, out_path, title):
    """items: [(label, PIL.Image)]，按同一 crop 区域裁切后横向拼成对比条"""
    panels = []
    x0, y0, x1, y1 = crop
    for label, im in items:
        p = im.crop(crop).resize(((x1 - x0) * zoom, (y1 - y0) * zoom), Image.NEAREST)
        panels.append((label, p))
    w, h = panels[0][1].size
    bar, pad = 34, 12
    W = w * len(panels) + pad * (len(panels) + 1)
    H = h + bar + pad * 2 + 40
    canvas = Image.new("RGB", (W, H), (245, 245, 243))
    d = ImageDraw.Draw(canvas)
    d.text((pad, 12), title, fill=(40, 40, 40), font=_font(20))
    for i, (label, p) in enumerate(panels):
        x = pad + i * (w + pad)
        y = 40 + bar
        canvas.paste(p, (x, y))
        d.rectangle([x, y - bar, x + w, y], fill=(228, 228, 224))
        d.text((x + 8, y - bar + 8), label, fill=(30, 30, 30), font=_font(18))
        d.rectangle([x, y - bar, x + w - 1, y + h - 1], outline=(200, 200, 196))
    canvas.save(out_path)
    return out_path


# ----------------------------- 主流程 ----------------------------- #
DEFAULT_PLAN = [
    ("art", "art", "medium"),
    ("art", "art-hd", "medium"),
    ("art", "anime", "none"),
    ("art", "esrgan", "none"),
    ("photo", "photo", "medium"),
    ("photo", "esrgan", "none"),
    ("photo", "art", "medium"),
    ("photo_noisy", "photo", "none"),
    ("photo_noisy", "photo", "medium"),
    ("photo_noisy", "photo", "highest"),
]

# 样本 -> (高清标尺, 低分输入)
SAMPLES = {
    "art": ("gt_art.png", "lr_art.png"),
    "photo": ("gt_photo.png", "lr_photo.png"),
    "photo_noisy": ("gt_photo.png", "lr_photo_noisy.png"),
}

CROPS = {
    "art": (150, 300, 430, 420),      # 细排线 + 色块边界
    "photo": (250, 300, 530, 420),    # 水面波纹 + 细草
    "photo_noisy": (250, 300, 530, 420),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=4, choices=[2, 4, 8, 16])
    ap.add_argument("--tile", type=int, default=192)
    ap.add_argument("--device", default="auto")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)

    rows = []
    for sample, preset, denoise in DEFAULT_PLAN:
        gt_name, lr_name = SAMPLES[sample]
        gt = Image.open(os.path.join(SAMP, gt_name)).convert("RGB")
        lr = Image.open(os.path.join(SAMP, lr_name)).convert("RGB")
        tw, th = gt.size
        gt_a = np.asarray(gt, np.float32) / 255.0
        lr_a = np.asarray(lr, np.float32) / 255.0

        # 双三次插值基线：低分图直接拉伸到目标尺寸
        bic = Image.fromarray((lr_a * 255 + 0.5).astype(np.uint8)).resize((tw, th), Image.LANCZOS)
        bic_a = np.asarray(bic, np.float32) / 255.0
        base = dict(psnr=psnr(bic_a, gt_a), ssim=ssim(bic_a, gt_a), t=0.0)

        t0 = time.time()
        ai = U.upscale(lr_a, preset=preset, scale=args.scale, denoise=denoise,
                       device=args.device, tile=args.tile, overlap=16)
        if ai.shape[0] != th or ai.shape[1] != tw:
            aim = Image.fromarray((ai * 255 + 0.5).astype(np.uint8)).resize((tw, th), Image.LANCZOS)
            ai = np.asarray(aim, np.float32) / 255.0
        el = time.time() - t0
        ai_p, ai_s = psnr(ai, gt_a), ssim(ai, gt_a)

        ai_img = Image.fromarray((ai * 255 + 0.5).astype(np.uint8))
        bic_img = bic.convert("RGB")
        low_img = lr.resize((tw, th), Image.NEAREST)

        tag = f"{sample}_{preset}_{denoise}"
        strip(
            [("低分图（最近邻，原始像素块）", low_img),
             ("双三次插值 Bicubic", bic_img),
             (f"AI 超分 · {preset}", ai_img)],
            CROPS[sample], 3, os.path.join(OUT, f"cmp_{tag}.png"),
            f"{'卡通/插画' if sample == 'art' else '照片'} · {args.scale}x · 低分 {lr.size[0]}x{lr.size[1]} -> {tw}x{th}")

        rows.append((sample, preset, denoise, base, ai_p, ai_s, el))
        print(f"  {sample:6s} {preset:8s} dn={denoise:7s} "
              f"PSNR {base['psnr']:6.2f} -> {ai_p:6.2f} dB (+{ai_p-base['psnr']:5.2f})   "
              f"SSIM {base['ssim']:.4f} -> {ai_s:.4f}   {el:5.1f}s", flush=True)

    print("\n" + "=" * 96)
    print(f"{'样本':<8}{'预设':<10}{'降噪':<10}{'PSNR(双三次)':>14}{'PSNR(AI)':>12}{'提升':>9}"
          f"{'SSIM(双三次)':>15}{'SSIM(AI)':>12}{'耗时':>9}")
    print("-" * 96)
    for s, p, dn, b, ap_, as_, el in rows:
        print(f"{s:<8}{p:<10}{dn:<10}{b['psnr']:>14.2f}{ap_:>12.2f}{ap_-b['psnr']:>+9.2f}"
              f"{b['ssim']:>15.4f}{as_:>12.4f}{el:>8.1f}s")
    print("=" * 96)

    # ---- 门禁：光打印不断言等于没测 ----
    # 原本这里只出表，「插画要比双三次高 2dB」「降噪档要单调」两条规矩全靠人眼看，
    # 改坏了也不会红。补上，让 bench 真的能拦住回归。
    bad: list[str] = []
    # 只对插画样本立这条规矩。照片样本上 PSNR 本来就不是合适的尺子 —— 感知型模型
    # 会「造」一些原图没有的纹理，对着干净 GT 算 PSNR 反而不如双三次，
    # 拿它当门禁只会天天误报。
    for s in sorted({r[0] for r in rows if r[0] == "art"}):
        gains = [r[4] - r[3]["psnr"] for r in rows if r[0] == s]
        if max(gains) < 2.0:
            bad.append(f"{s}：最好的预设也只比双三次高 {max(gains):+.2f} dB（要求 ≥ +2.00）")

    # 降噪档单调：只在带噪样本上说得通（干净图加大降噪本来就可能糊）。
    ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3, "highest": 4}
    for key in sorted({(r[0], r[1]) for r in rows if "noisy" in r[0]}):
        seq = sorted((ORDER[r[2]], r[4] - r[3]["psnr"]) for r in rows if (r[0], r[1]) == key)
        vals = [g for _, g in seq]
        if any(b < a - 0.05 for a, b in zip(vals, vals[1:])):
            bad.append(f"{key[0]}/{key[1]}：降噪档不单调 "
                       f"{['%+.2f' % v for v in vals]}（降噪越强不该越差）")

    if bad:
        print("\n不达标：")
        for b in bad:
            print(f"  ✗ {b}")
        print("\n对比图已输出到 " + OUT)
        sys.exit(1)
    print("门禁通过：插画样本有明显增益、带噪样本降噪档单调")
    print(f"对比图已输出到 {OUT}")


if __name__ == "__main__":
    main()
