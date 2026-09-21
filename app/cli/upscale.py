#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""BigPixels 放大台 —— 命令行入口。

原理与线上服务一致：
    低分图 --（必要时先插值放大）--> 深度卷积网络预测修正 --> 高清大图

权重换成开源版本，共两族：
    waifu2x（CUnet / Swin-UNet）—— 就是线上那个 bigjpg 用的那一族，art/photo 双域 + noise0~3 四档降噪
    Real-ESRGAN x4plus / 4x-AnimeSharp —— 现代 GAN 系，照片与动漫线稿更强

核心实现拆在 app/core/ 下，这里只管参数解析和进度显示：
    core/presets.py   预设 / 降噪 / 清晰度 → 权重文件与锐化参数
    core/runner.py    单个 ONNX 模型的瓦片式推理器
    core/pipeline.py  串联多次网络 + 收尾缩放 + 收尾锐化
    core/imaging.py   读图 / 存图 / unsharp
    core/paths.py     项目里所有路径的唯一出处

用法示例：
    python tools/upscale.py in.jpg -o out.png --preset art --denoise medium --scale 4
    python tools/upscale.py in.jpg -o out.png --clear crisp        # 线条更硬，接近线上那种观感
    python tools/upscale.py D:\\pics -o D:\\out --preset photo --scale 2
    python tools/upscale.py --list
    python tools/upscale.py --info
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np
from PIL import Image

# 直接跑这个文件（python app/cli/upscale.py）时项目根不在 sys.path 上，
# 得自己挂上去，否则 app.core 导不进来。
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.core import (                     # noqa: E402
    BRIGHT_LEVELS, CLEAR_LEVELS, DENOISE_LEVELS, MODEL_DIR, PRESETS, SRRunner,
    brighten, load_image, ort, plan_passes, resolve_bright, resolve_model,
    resolve_sharpen, save_image, unsharp, upscale_alpha,
)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def _outname(name: str, scale: int) -> str:
    stem, ext = os.path.splitext(name)
    if ext.lower() in (".jpg", ".jpeg"):
        ext = ".png"          # 放大结果转 PNG，避免二次 JPEG 损失
    return f"{stem}_{scale}x{ext}"


def _print_info():
    print("设备：")
    print("  可用 provider：", ort.get_available_providers())
    print("  CPU 核心：", os.cpu_count())
    print("\n预设（原生倍率以运行时自动探测为准，这里给的是已知值）：")
    for k, v in PRESETS.items():
        files = list(v["noise"].values())
        have = sum(1 for f in files if os.path.isfile(os.path.join(MODEL_DIR, f)))
        # 1x 降噪那一档一律标「不可用」：上游的 scale1x.onnx 是个 1542 字节的占位文件，
        # 文件在也跑不出降噪（实测输出 == 输入）。别再按「文件在不在」报「有」了。
        print(f"  {k:8s} ~{v['hint']}x  [{have}/{len(files)} 档权重就绪]  "
              f"{v['desc']}  ({v.get('tech', '')})")
    print("\n本地模型：")
    if os.path.isdir(MODEL_DIR):
        tot = 0
        for f in sorted(os.listdir(MODEL_DIR)):
            if f.endswith(".onnx"):
                sz = os.path.getsize(os.path.join(MODEL_DIR, f))
                tot += sz
                print(f"  {sz/1048576:7.2f}MB  {f}")
        print(f"  合计 {tot/1048576:.1f}MB")
    else:
        print("  （还没下载，运行 python tools/download_models.py）")


def main(argv=None):
    ap = argparse.ArgumentParser(description="BigPixels：深度卷积网络放大 2/4/8/16 倍")
    ap.add_argument("input", nargs="?", help="输入图片或目录")
    ap.add_argument("-o", "--output", help="输出图片或目录")
    ap.add_argument("--preset", default="anime", help="预设：" + "/".join(PRESETS))
    ap.add_argument("--scale", type=int, default=4, choices=[1, 2, 4, 8, 16], help="放大倍率")
    ap.add_argument("--denoise", default="medium", help="降噪程度：" + "/".join(DENOISE_LEVELS))
    ap.add_argument("--clear", default="normal",
                    help="清晰度（收尾锐化）：" + "/".join(CLEAR_LEVELS) + "，也可直接给增益数字")
    ap.add_argument("--bright", default="off",
                    help="明暗：" + "/".join(BRIGHT_LEVELS)
                         + "。lift = 整体 ×1.019，换线上那种通透观感，代价是偏离原图一点；"
                           "也可直接给乘数数字")
    ap.add_argument("--tile", type=int, default=256, help="瓦片边长（显存不够就调小）")
    ap.add_argument("--overlap", type=int, default=16, help="瓦片重叠像素")
    ap.add_argument("--device", default="auto", choices=["auto", "dml", "cpu"])
    ap.add_argument("--compare", action="store_true", help="同时输出双三次插值对照图")
    ap.add_argument("--list", action="store_true", help="列出预设")
    ap.add_argument("--info", action="store_true", help="显示设备与模型状态")
    args = ap.parse_args(argv)

    # --clear 允许直接给数字（调参时最省事）：这里把数字串转成 float，
    # 否则 "4.0" 会被当成档位名、报「未知清晰度 '4.0'」——帮助里写着能用就不该挡。
    if args.clear not in CLEAR_LEVELS:
        try:
            args.clear = float(args.clear)
        except ValueError:
            raise SystemExit(f"未知清晰度 '{args.clear}'，可选：{', '.join(CLEAR_LEVELS)}，或直接给数字（如 2.5）")

    # --bright 同理：允许直接给乘数
    if args.bright not in BRIGHT_LEVELS:
        try:
            args.bright = float(args.bright)
        except ValueError:
            raise SystemExit(f"未知明暗 '{args.bright}'，可选：{', '.join(BRIGHT_LEVELS)}，或直接给乘数（如 1.03）")

    if args.list:
        for k, v in PRESETS.items():
            print(f"{k:8s} ~{v['hint']}x  {v['desc']}  ({v.get('tech', '')})")
            for idx, f in v["noise"].items():
                print(f"          降噪档 {idx} -> {f}")
            r, g = v.get("sharp", (0, 0))
            print("          清晰度 标准 -> 半径 %.1fpx / 增益 %.2f" % (r, g))
        print("\n清晰度档位：" + " / ".join(
            f"{k}(×{v:g})" for k, v in CLEAR_LEVELS.items()))
        print("明暗档位：" + " / ".join(
            f"{k}(×{v:g})" for k, v in BRIGHT_LEVELS.items())
            + " —— 默认原样最贴原图，lift 是线上那种通透感（会偏离原图一点）")
        return
    if args.info:
        _print_info()
        return
    if not args.input:
        ap.print_help()
        return

    src = args.input
    if os.path.isdir(src):
        files = [os.path.join(src, f) for f in sorted(os.listdir(src))
                 if os.path.splitext(f)[1].lower() in IMG_EXT]
        outdir = args.output or os.path.join(ROOT, "outputs")
        targets = [(f, os.path.join(outdir, _outname(os.path.basename(f), args.scale)))
                   for f in files]
    else:
        out = args.output or os.path.join(ROOT, "outputs",
                                         _outname(os.path.basename(src), args.scale))
        targets = [(src, out)]
    if not targets:
        raise SystemExit("没找到可处理的图片")

    path = resolve_model(args.preset, args.denoise, args.scale)
    runner = SRRunner(path, device=args.device, tile=args.tile, overlap=args.overlap)
    passes, net_scale = plan_passes(args.scale, runner.scale)
    print(f"模型 {runner.name}")
    print(f"  设备 {runner.device.upper()} | 网络原生 {runner.scale}x | 目标 {args.scale}x "
          f"| 串联 {passes} 次 | 外扩 {runner.pad}px / 裁回 {runner.crop}px | 瓦片 {runner.tile}(+{runner.overlap})")
    _r, _g = resolve_sharpen(args.preset, args.clear)
    _label = args.clear if isinstance(args.clear, str) else "自定义"
    print(f"  清晰度 {_label}（锐化半径 {_r}px · 增益 {_g:.2f}）")
    _b = resolve_bright(args.bright)
    _blabel = args.bright if isinstance(args.bright, str) else "自定义"
    print(f"  明暗 {_blabel}（整体 ×{_b:.3f}）")

    for i, (f, out) in enumerate(targets, 1):
        rgb, alpha, mode = load_image(f)
        h0, w0 = rgb.shape[:2]
        print(f"[{i}/{len(targets)}] {os.path.basename(f)}  {w0}x{h0}（{mode}）")
        t0 = time.time()
        cur = rgb
        for p in range(passes):
            state = {"last": -1}

            def prog(done, total, el, _bar=state):
                pct = int(done * 100 / total)
                if pct >= _bar["last"] + 10 or done == total:
                    _bar["last"] = pct
                    eta = (el / done) * (total - done) if done else 0
                    print(f"    瓦片 {done}/{total}  {pct:3d}%  已用 {el:5.1f}s  还剩约 {eta:5.1f}s",
                          flush=True)

            if passes > 1:
                print(f"    网络推理 {p + 1}/{passes}")
            cur = runner.upscale(cur, progress=prog)
        if net_scale != args.scale:
            th, tw = h0 * args.scale, w0 * args.scale
            cur = np.asarray(Image.fromarray((cur * 255 + 0.5).astype(np.uint8))
                             .resize((tw, th), Image.LANCZOS), np.float32) / 255.0
        radius, gain = resolve_sharpen(args.preset, args.clear)
        if gain > 0:
            print(f"    收尾锐化 半径 {radius}px · 增益 {gain:.2f}")
            cur = unsharp(cur, radius, gain)
        kb = resolve_bright(args.bright)
        if abs(kb - 1.0) > 1e-9:
            print(f"    明暗 整体 ×{kb:.3f}")
            cur = brighten(cur, kb)
        save_image(cur, out, upscale_alpha(alpha, args.scale))
        print(f"    -> {out}  {cur.shape[1]}x{cur.shape[0]}  用时 {time.time()-t0:.1f}s")

        if args.compare:
            base = Image.fromarray((rgb * 255 + 0.5).astype(np.uint8)).resize(
                (cur.shape[1], cur.shape[0]), Image.LANCZOS).convert("RGB")
            cmp_path = os.path.splitext(out)[0] + "_bicubic.jpg"
            base.save(cmp_path, quality=95, subsampling=0)
            print(f"    -> {cmp_path}（双三次插值对照）")


if __name__ == "__main__":
    main()
