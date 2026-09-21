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
#   实测（351×1295 的动漫插画 → 4x，拉普拉斯方差，越大越锐）：
#       双三次/Lanczos 基线 9.8 · art 原样 100 · art+锐化 872 · bigjpg 4x 卡通 914
#   锐化只在边缘起作用：平坦区 32×32 块的 std 始终 0.00，不会把干净的地方磨出噪点。
#   照片档给得保守（真实噪声会被一起放大），插画档给得足。
PRESETS: dict[str, dict] = {
    "art": {
        "label": "卡通 / 插画",
        "desc": "卡通、插画、线稿都能用，速度最快",
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
        "desc": "插画专用，比上一档更干净，也更慢",
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
    "esrgan": {
        "label": "通用场景",
        "desc": "照片和插画都行，适用范围最广",
        "tech": "Real-ESRGAN x4plus · 原生 4× · 68 MB · 单权重，无降噪档",
        "noise": {0: "RealESRGAN_x4plus.onnx"},
        "dn_only": None,
        "hint": 4,
        "sharp": (0.8, 0.6),
    },
    "anime": {
        "label": "动漫线稿",
        "desc": "二次元线稿专用，线条最硬",
        "tech": "4x-AnimeSharp · 原生 4× · 68 MB · 单权重，无降噪档",
        "noise": {0: "4x-AnimeSharp.onnx"},
        "dn_only": None,
        "hint": 4,
        "sharp": (1.0, 0.85),
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
        if not p.get("dn_only"):
            raise SystemExit(f"预设 '{preset}' 没有“只降噪”权重，无法 1x 处理")
        path = os.path.join(MODEL_DIR, p["dn_only"])
    else:
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


