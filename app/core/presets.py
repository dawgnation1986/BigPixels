#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模型注册表与档位解析：预设 / 降噪 / 清晰度 → 具体权重文件名和锐化参数。"""
from __future__ import annotations

import math
import os

from .paths import MODEL_DIR


# --------------------------------------------------------------------------- #
# 模型注册表
# --------------------------------------------------------------------------- #
# noise: 降噪程度档位。无/低/中/高/最高 -> 0/1/2/3/3
#        上游 waifu2x 只发布 noise0~3 四组权重，“最高”回落到 noise3。
#
# sharp: 收尾锐化的基准强度 (半径 px, 增益)。增益 = 该像素比高斯模糊版多出来的量乘几倍。
#   为什么需要它：网络出来的边缘天然是「渐变」的。waifu2x CUnet 尤其软 ——
#   4x 之后一条 1px 的线会摊成 4~7px 的灰带，细线整个糊掉。
#   锐化只在边缘起作用：平坦区 32×32 块的 std 始终 0.00，不会把干净的地方磨出噪点。
#   照片档给得保守（真实噪声会被一起放大），插画档给得足。
#
#   ⚠️ 但锐化只是补救，**主因是选哪个预训练权重**。同一张 351×1295 动漫插画 → 4x，
#   对线上 bigjpg 的 4x 卡通（细节热点区的拉普拉斯方差 = 3279，当作 100%）：
#       动漫插画 4x-AnimeSharp（原生 4× 一次推理） 3512 → 107%
#       卡通/插画 waifu2x CUnet（2× 串联两次）     1133 →  35%
#       通用场景 Real-ESRGAN x4plus                1219 →  37%
#       插画高清 waifu2x Swin（2× 串联两次）        936 →  29%
#   差的这 3 倍锐化追不回来 —— 把增益往上加只会连对比度和噪点一起抬起来
#   （实测增益 2 → 6：锐度到 2241，但局部对比度 62.9 vs bigjpg 41.3、平块噪声 ×3）。
#   所以对二次元插画**该选 anime 而不是 art**；串联两次的 2× 网络本身就在丢细节。
#   注：下面 sharp 的数值是「锐化该给多少」，与上面这个「选哪个权重」是两回事。
PRESETS: dict[str, dict] = {
    "anime": {
        "label": "动漫插画",
        "desc": "彩色二次元插画 / 原画 —— 细节保留最多、线条最硬，走 GPU 还最快",
        "tech": "4x-AnimeSharp · 原生 4× · 68 MB · 单权重，无降噪档",
        "noise": {0: "4x-AnimeSharp.onnx"},
        "dn_only": None,
        "hint": 4,
        # 半径 0.85 / 基准 1.30：
        #   · 增益 1.30 是照线上 bigjpg 的 4x 卡通校准的 —— 标准档落在眼睛 100% / 脸 97%。
        #   · 半径从 1.0 收到 0.85 是拿原图当尺子扫出来的：同观感下对原图 PSNR
        #     32.71 → 32.99（更锐档 31.81 → 32.21）。半径小 = 只提细节、不强推边缘，
        #     所以「还原度上去、观感不动」这两个目标能同时满足。
        #   · 1.0px 的半径在 4x 输出上已经摸到「推强边」那一档，是虚高的来源。
        "sharp": (0.85, 1.30),
    },
    "art": {
        "label": "卡通 / 插画（轻量）",
        "desc": "waifu2x 老模型，结果偏「软」偏保守；只能 CPU 跑，反而比上一档慢",
        "tech": "waifu2x CUnet · 原生 2× · 4.9 MB/档 · 带四档降噪权重",
        "noise": {
            0: "waifu2x_cunet_art_noise0_2x.onnx",
            1: "waifu2x_cunet_art_noise1_2x.onnx",
            2: "waifu2x_cunet_art_noise2_2x.onnx",
            3: "waifu2x_cunet_art_noise3_2x.onnx",
        },
        "dn_only": "waifu2x_cunet_art_dn1x.onnx",
        "hint": 2,
        "sharp": (1.3, 2.0),
    },
    "art-hd": {
        "label": "插画 高清",
        "desc": "waifu2x Swin 版，比「轻量」干净一点，同样是 2× 串联",
        "tech": "waifu2x Swin-UNet · 原生 2× · 16 MB/档 · 带四档降噪权重",
        "noise": {
            0: "waifu2x_swin_art_noise0_2x.onnx",
            1: "waifu2x_swin_art_noise1_2x.onnx",
            2: "waifu2x_swin_art_noise2_2x.onnx",
            3: "waifu2x_swin_art_noise3_2x.onnx",
        },
        "dn_only": None,
        "hint": 2,
        "sharp": (1.1, 1.6),
    },
    "esrgan": {
        "label": "通用场景",
        "desc": "照片和插画都行，最不容易出错；细节不如「动漫插画」",
        "tech": "Real-ESRGAN x4plus · 原生 4× · 68 MB · 单权重，无降噪档",
        "noise": {0: "RealESRGAN_x4plus.onnx"},
        "dn_only": None,
        "hint": 4,
        "sharp": (0.8, 0.6),
    },
    "photo": {
        "label": "照片 / 实拍",
        "desc": "真实照片，输出偏保守",
        "tech": "waifu2x Swin-UNet photo 域 · 原生 2× · 18 MB/档",
        "noise": {
            0: "waifu2x_swin_photo_noise0_2x.onnx",
            1: "waifu2x_swin_photo_noise1_2x.onnx",
            2: "waifu2x_swin_photo_noise2_2x.onnx",
            3: "waifu2x_swin_photo_noise3_2x.onnx",
        },
        "dn_only": None,
        "hint": 2,
        "sharp": (0.8, 0.6),
    },
}

DENOISE_LEVELS = {"none": 0, "low": 1, "medium": 2, "high": 3, "highest": 3}

# 清晰度档位 -> 在预设基准锐度上再乘一个系数。
# 这三档是给用户「我要更接近线上那种硬线条」的入口，原样就是完全不做锐化。
CLEAR_LEVELS = {"soft": 0.0, "normal": 1.0, "crisp": 1.5}
CLEAR_LABELS = {"soft": "原样", "normal": "标准", "crisp": "更锐"}


def resolve_model(preset: str, denoise: str, scale: int = 2) -> str:
    if preset not in PRESETS:
        raise SystemExit(f"未知预设 '{preset}'，可选：{', '.join(PRESETS)}")
    p = PRESETS[preset]
    if scale == 1:
        # 「只降噪」用不了：上游 deepghs/waifu2x_onnx 的 cunet/art/scale1x.onnx
        # 实际是个 1542 字节的占位文件 —— 它不是模型。跑出来的东西跟输入逐像素相同
        # （实测强噪声进去、std 一动不动），也就是**静默空转**。
        # 与其给用户一张「看着成功、其实没降噪」的图，不如在这儿说清楚。
        if not p.get("dn_only"):
            raise SystemExit(f"预设 '{preset}' 没有 1x 权重，无法只降噪")
        raise SystemExit(
            "1x「只降噪」这一档用不了：上游的 scale1x.onnx 是个 1542 字节的占位文件，"
            "不是真模型（见 README「已知问题」）。\n"
            "  要降噪就放大到 2x 及以上，用 --denoise 选降噪档 —— 那几档是好的。")
    idx = DENOISE_LEVELS.get(denoise)
    if idx is None:
        raise SystemExit(f"未知降噪档 '{denoise}'，可选：{', '.join(DENOISE_LEVELS)}")
    if idx not in p["noise"]:
        idx = max(p["noise"])
    path = os.path.join(MODEL_DIR, p["noise"][idx])
    if not os.path.isfile(path):
        raise SystemExit(f"模型文件不存在：{path}\n请先运行 python download_models.py")
    return path


def plan_passes(scale: int, native: int) -> tuple[int, int]:
    """要串几次网络 + 网络实际输出倍率（不足或超出部分最后用 Lanczos 收尾）"""
    if scale == 1:
        if native != 1:
            raise SystemExit("所选模型不是 1x 模型，无法只降噪；请用 --scale 2 起")
        return 1, 1
    if native == 1:
        raise SystemExit("所选模型只能 1x 降噪，不能放大")
    k = max(1, int(math.ceil(math.log(scale) / math.log(native) - 1e-9)))
    full = native ** k
    if full == scale:
        return k, full
    if k > 1 and (native ** (k - 1)) * 2 >= scale:
        return k - 1, native ** (k - 1)
    return k, full


def resolve_sharpen(preset: str, clear: str | float) -> tuple[float, float]:
    """清晰度 -> 实际用的 (半径, 增益)。传数字就是直接指定增益。"""
    if isinstance(clear, (int, float)):
        gain = float(clear)
    else:
        mult = CLEAR_LEVELS.get(clear)
        if mult is None:
            raise SystemExit(f"未知清晰度 '{clear}'，可选：{', '.join(CLEAR_LEVELS)}")
        gain = PRESETS[preset].get("sharp", (0.0, 0.0))[1] * mult
    radius = PRESETS[preset].get("sharp", (0.0, 0.0))[0]
    return radius, gain


