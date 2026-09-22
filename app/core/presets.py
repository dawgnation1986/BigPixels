#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模型注册表与档位解析：预设 / 降噪 / 清晰度 → 具体权重文件名和锐化参数。

给用户看的名字和说明不在这里 —— 那些在 app/locales/<lang>.json，
按 `preset.<id>.label|desc|tech`、`denoise.<id>`、`clear.<id>.label|desc`、
`bright.<id>.label|desc` 取。这张表只放"哪个档用哪个文件、锐化给多少"。
"""
from __future__ import annotations

import math
import os

from .i18n import t
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
        "noise": {0: "RealESRGAN_x4plus.onnx"},
        "dn_only": None,
        "hint": 4,
        "sharp": (0.8, 0.6),
    },
    "photo": {
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

# 明暗档：本质是个开关，只有「原样 / 提亮」两档。
# 1.019 是量出来的，不是拍的 —— 线上 bigjpg 卡通/插画 4x 的平坦区拟合下来是
# 乘法增益 ×1.0192（亮部 +4.8 灰阶、中间调 +1.1）。三种拟合里乘法残差最小。
# 说清楚代价：这一档会让输出**更不像原图**（对原图 PSNR 会掉），
# 换的是「线上那种通透观感」。默认关。
BRIGHT_LEVELS = {"off": 1.0, "lift": 1.019}


def label(kind: str, code: str) -> str:
    """档位的中文/英文/日文名。kind 是 preset / denoise / clear / bright。"""
    return t(f"{kind}.{code}.label")


def resolve_model(preset: str, denoise: str, scale: int = 2) -> str:
    if preset not in PRESETS:
        raise SystemExit(t("err.preset", name=preset, list=", ".join(PRESETS)))
    p = PRESETS[preset]
    if scale == 1:
        # 「只降噪」用不了：上游 deepghs/waifu2x_onnx 的 cunet/art/scale1x.onnx
        # 实际是个 1542 字节的占位文件 —— 它不是模型。跑出来的东西跟输入逐像素相同
        # （实测强噪声进去、std 一动不动），也就是**静默空转**。
        # 与其给用户一张「看着成功、其实没降噪」的图，不如在这儿说清楚。
        if not p.get("dn_only"):
            raise SystemExit(t("err.preset_no_dn1x", name=preset))
        raise SystemExit(t("err.dn1x_placeholder"))
    idx = DENOISE_LEVELS.get(denoise)
    if idx is None:
        raise SystemExit(t("err.denoise", name=denoise, list=", ".join(DENOISE_LEVELS)))
    if idx not in p["noise"]:
        idx = max(p["noise"])
    path = os.path.join(MODEL_DIR, p["noise"][idx])
    if not os.path.isfile(path):
        raise SystemExit(t("err.model_missing", path=path))
    return path


def plan_passes(scale: int, native: int) -> tuple[int, int]:
    """要串几次网络 + 网络实际输出倍率（不足或超出部分最后用 Lanczos 收尾）"""
    if scale == 1:
        if native != 1:
            raise SystemExit(t("err.not_1x_model"))
        return 1, 1
    if native == 1:
        raise SystemExit(t("err.only_1x"))
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
            raise SystemExit(t("err.clear", name=clear, list=", ".join(CLEAR_LEVELS)))
        gain = PRESETS[preset].get("sharp", (0.0, 0.0))[1] * mult
    radius = PRESETS[preset].get("sharp", (0.0, 0.0))[0]
    return radius, gain


def resolve_bright(bright: str | float) -> float:
    """明暗档 -> 乘数。传数字就是直接指定乘数。

    这一档跟预设无关（是个全局的观感开关），所以不需要 preset 参数。
    """
    if isinstance(bright, (int, float)):
        return float(bright)
    f = BRIGHT_LEVELS.get(bright)
    if f is None:
        raise SystemExit(t("err.bright", name=bright, list=", ".join(BRIGHT_LEVELS)))
    return f
