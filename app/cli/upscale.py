#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""BigPixels 放大台 —— 命令行入口。

原理与线上服务一致：
    低分图 --（必要时先插值放大）--> 深度卷积网络预测修正 --> 高清大图

权重换成开源版本，共两族：
    waifu2x（CUnet / Swin-UNet）—— 与线上 bigjpg 同属一族，art/photo 双域 + noise0~3 四档降噪
    Real-ESRGAN x4plus / 4x-AnimeSharp —— 现代 GAN 系，照片与动漫线稿更强

核心实现拆在 app/core/ 下，这里只管参数解析和进度显示：
    core/presets.py   预设 / 降噪 / 清晰度 → 权重文件与收尾参数
    core/runner.py    单个 ONNX 模型的瓦片式推理器
    core/pipeline.py  串联多次网络 + 收尾缩放 + 收尾细节补偿
    core/imaging.py   读图 / 存图 / unsharp / detail_band
    core/paths.py     项目里所有路径的唯一出处

用法示例：
    python tools/upscale.py in.jpg -o out.png --preset art --denoise medium --scale 4
    python tools/upscale.py in.jpg -o out.png --clear crisp        # 线条更硬，接近线上观感
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
    brighten, declip, detail_band, load_image, ort, plan_runs, resolve_bright,
    resolve_finish, resolve_model, save_image, unsharp, upscale_alpha,
)
from app.core.i18n import t, set_lang       # noqa: E402


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
    print(t("cli.device"))
    print(t("cli.provider"), ort.get_available_providers())
    print(t("cli.cpu"), os.cpu_count())
    print("\n" + t("cli.presets"))
    for k, v in PRESETS.items():
        files = list(v["noise"].values())
        have = sum(1 for f in files if os.path.isfile(os.path.join(MODEL_DIR, f)))
        # 1x 降噪那一档一律标「不可用」：上游的 scale1x.onnx 是个 1542 字节的占位文件，
        # 文件在也跑不出降噪（实测输出 == 输入）。别再按「文件在不在」报「有」了。
        print(t("cli.preset.row", k=k, hint=v["hint"], have=have, total=len(files),
                desc=t(f"preset.{k}.desc"), tech=t(f"preset.{k}.tech")))
    print("\n" + t("cli.models"))
    if os.path.isdir(MODEL_DIR):
        tot = 0
        for f in sorted(os.listdir(MODEL_DIR)):
            if f.endswith(".onnx"):
                sz = os.path.getsize(os.path.join(MODEL_DIR, f))
                tot += sz
                print(f"  {sz/1048576:7.2f}MB  {f}")
        print(t("cli.total", size=f"{tot/1048576:.1f}MB"))
    else:
        print(t("cli.nomodel"))


def main(argv=None):
    ap = argparse.ArgumentParser(description=t("cli.desc"))
    ap.add_argument("input", nargs="?", help=t("cli.arg.input"))
    ap.add_argument("-o", "--output", help=t("cli.arg.output"))
    ap.add_argument("--preset", default="anime",
                    help=t("cli.arg.preset", list="/".join(PRESETS)))
    ap.add_argument("--scale", type=int, default=4, choices=[1, 2, 4, 8, 16],
                    help=t("cli.arg.scale"))
    ap.add_argument("--denoise", default="medium",
                    help=t("cli.arg.denoise", list="/".join(DENOISE_LEVELS)))
    ap.add_argument("--clear", default="normal",
                    help=t("cli.arg.clear", list="/".join(CLEAR_LEVELS)))
    ap.add_argument("--bright", default="off",
                    help=t("cli.arg.bright", list="/".join(BRIGHT_LEVELS)))
    ap.add_argument("--tile", type=int, default=256, help=t("cli.arg.tile"))
    ap.add_argument("--overlap", type=int, default=16, help=t("cli.arg.overlap"))
    ap.add_argument("--device", default="auto", choices=["auto", "dml", "cpu"],
                    help=t("cli.arg.device"))
    ap.add_argument("--compare", action="store_true", help=t("cli.arg.compare"))
    ap.add_argument("--list", action="store_true", help=t("cli.arg.list"))
    ap.add_argument("--info", action="store_true", help=t("cli.arg.info"))
    ap.add_argument("--lang", choices=("zh", "en", "ja"), default=None,
                    help=t("lang.arghelp"))
    args = ap.parse_args(argv)

    if args.lang:
        set_lang(args.lang)

    # --clear 允许直接给数字（调参时最省事）：这里把数字串转成 float，
    # 否则 "4.0" 会被当成档位名、报「未知清晰度 '4.0'」——帮助里写着能用就不该挡。
    if args.clear not in CLEAR_LEVELS:
        try:
            args.clear = float(args.clear)
        except ValueError:
            raise SystemExit(t("err.clear_num", name=args.clear,
                                list=", ".join(CLEAR_LEVELS)))

    # --bright 同理：允许直接给乘数
    if args.bright not in BRIGHT_LEVELS:
        try:
            args.bright = float(args.bright)
        except ValueError:
            raise SystemExit(t("err.bright_num", name=args.bright,
                                list=", ".join(BRIGHT_LEVELS)))

    if args.list:
        for k, v in PRESETS.items():
            print(t("cli.list.preset", k=k, hint=v["hint"],
                    desc=t(f"preset.{k}.desc"), tech=t(f"preset.{k}.tech")))
            for idx, f in v["noise"].items():
                print(t("cli.list.denoise", idx=idx, f=f))
            if "band" in v:
                b1, b2, bg = v["band"]
                print(t("cli.list.band", r1=b1, r2=b2, g=bg))
            else:
                r, g = v.get("sharp", (0, 0))
                print(t("cli.list.sharp", r=r, g=g))
        print("\n" + t("cli.list.clear") + " / ".join(
            f"{k}(×{v:g})" for k, v in CLEAR_LEVELS.items()))
        print(t("cli.list.bright", levels=" / ".join(
            f"{k}(×{v:g})" for k, v in BRIGHT_LEVELS.items())))
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
        raise SystemExit(t("err.no_image"))

    path = resolve_model(args.preset, args.denoise, args.scale)
    runner = SRRunner(path, device=args.device, tile=args.tile, overlap=args.overlap)
    runs, net_scale = plan_runs(args.preset, args.scale, runner.scale)
    passes = len(runs)
    print(t("cli.run.model", name=runner.name))
    print(t("cli.run.summary", device=runner.device.upper(), native=runner.scale,
            scale=args.scale, passes=passes, pad=runner.pad, crop=runner.crop,
            tile=runner.tile, overlap=runner.overlap))
    _kind, _r1, _r2, _g = resolve_finish(args.preset, args.clear, args.scale)
    _label = args.clear if isinstance(args.clear, str) else "自定义"
    _rtxt = "%.1f-%.1fpx" % (_r1, _r2) if _kind == "band" else "%.1fpx" % _r1
    print(t("cli.run.clear", label=_label, r=_rtxt, g=_g))
    _b = resolve_bright(args.bright)
    _blabel = args.bright if isinstance(args.bright, str) else "自定义"
    print(t("cli.run.bright", label=_blabel, b=_b))

    for i, (f, out) in enumerate(targets, 1):
        rgb, alpha, mode = load_image(f)
        h0, w0 = rgb.shape[:2]
        print(t("cli.run.file", i=i, n=len(targets), name=os.path.basename(f),
                w=w0, h=h0, mode=mode))
        t0 = time.time()
        # 进网络之前先把源图的 JPEG 振铃压掉，跟 app/core/pipeline.py 的 upscale()
        # 保持一致。位图和网页各写一套循环，这一步漏掉哪一边，同一张图就会出两种
        # 结果；自检 4d/4e 用「各入口的结果 == app.core.upscale」把三条路钉在一起。
        cur = declip(rgb)
        for p in range(passes):
            state = {"last": -1}

            def prog(done, total, el, _bar=state):
                pct = int(done * 100 / total)
                if pct >= _bar["last"] + 10 or done == total:
                    _bar["last"] = pct
                    eta = (el / done) * (total - done) if done else 0
                    print(t("cli.run.tile", done=done, total=total, pct=pct,
                            el=el, eta=eta), flush=True)

            if passes > 1:
                print(t("cli.run.net", p=p + 1, passes=passes))
            cur = runner.upscale(cur, progress=prog, post_down=runs[p])
        if net_scale != args.scale:
            th, tw = h0 * args.scale, w0 * args.scale
            cur = np.asarray(Image.fromarray((cur * 255 + 0.5).astype(np.uint8))
                             .resize((tw, th), Image.LANCZOS), np.float32) / 255.0
        kind, fr1, fr2, fgain = resolve_finish(args.preset, args.clear, args.scale)
        if fgain > 0:
            ftxt = "%.1f-%.1fpx" % (fr1, fr2) if kind == "band" else "%.1fpx" % fr1
            print(t("cli.run.sharpen", r=ftxt, g=fgain))
            cur = (detail_band(cur, fr1, fr2, fgain) if kind == "band"
                   else unsharp(cur, fr1, fgain))
        kb = resolve_bright(args.bright)
        if abs(kb - 1.0) > 1e-9:
            print(t("cli.run.bright.short", b=kb))
            cur = brighten(cur, kb)
        save_image(cur, out, upscale_alpha(alpha, args.scale))
        print(t("cli.run.done", out=out, w=cur.shape[1], h=cur.shape[0],
                sec=time.time() - t0))

        if args.compare:
            base = Image.fromarray((rgb * 255 + 0.5).astype(np.uint8)).resize(
                (cur.shape[1], cur.shape[0]), Image.LANCZOS).convert("RGB")
            cmp_path = os.path.splitext(out)[0] + "_bicubic.jpg"
            base.save(cmp_path, quality=95, subsampling=0)
            print(t("cli.run.bicubic", path=cmp_path))


if __name__ == "__main__":
    main()
