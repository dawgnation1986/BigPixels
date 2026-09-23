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
# sharp: 收尾锐化（USM）的基准强度 (半径 px, 增益)，老预设仍在用。
#   增益 = 该像素比高斯模糊版多出来的量乘几倍。
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
#   注：下面 sharp / band 的数值是「收尾该给多少」，与上面这个「选哪个权重」是两回事。
#
# band = 零均值带通补偿 (半径小, 半径大, 增益)，插画预设现在用它收尾。
#   unsharp 的高通里混着 1px 的振铃和噪点，抬它的同时会在每条线两侧各留一道
#   亮/暗边（光晕）；带通是两个高斯之差，零均值，抬的正好是「线宽」那一层。
#   真值回环量下来：带通对还原度是正的（甚至比不锐化还高一点），unsharp 是负的。
PRESETS: dict[str, dict] = {
    "anime": {
        "noise": {0: "4x-AnimeSharp.onnx"},
        "dn_only": None,
        "hint": 4,
        # band = (半径小, 半径大, 增益)，基准 4x；resolve_finish 按 scale/4 缩放半径。
        # 这三个数是三个数据集一起扫出来的（改前/改后都在同一批脚本里跑）：
        #   官方门禁样本   真作者图 4x 回环   真作者图 8x 回环
        #   (samples/gt_art.png)  (原始数据/微信图片，四块 512² 平均)
        #     什么都不做   +1.88 / 0.8771    28.60 / 0.9166    23.61 / 0.7905
        #     旧 unsharp   +2.10 / 0.8878    26.12 / 0.8900    —— 净负收益，且线两侧镶光晕
        #     0.9-2.7 g=.5 +2.08 / 0.8840    27.99 / 0.9159    23.83 / 0.8006
        #     1.0-3.0 g=.4 +2.06 / 0.8831    28.31 / 0.9194    23.83 / 0.8007   ← 选它
        #     1.0-3.0 g=.6 +2.05 / 0.8832    27.84 / 0.9163    23.80 / 0.8018
        # 挑 g=0.4 是因为它在真图 4x 上最还原（比 g=.5 高 0.22 dB），8x 上三家打平。
        # 半径为什么是 1.0-3.0：这一层正好是「线的宽度」。8x 上按 scale/4 放大成
        # 2.0-6.0 之后 SSIM 才从 0.7905 抬到 0.8007 —— 不缩放时 8x 比自家 4x 还软。
        "band": (1.0, 3.0, 0.40),
    },
    "optimized": {
        # 「优化算法版」：同一套权重，但**网络跑两次**。
        #   单次网络交出来的是「渐变边」—— 原图里确实存在的细线（例如嘴角那条轮廓）
        #   会被抹成一团渐变；再跑一次网络才会把它当结构重新画出来。同一块嘴实测：
        #     单次         → 下缘没有线
        #     大半径 USM   → 有线，但是一圈光晕，真值 PSNR 也赔掉 5 dB 多
        #     两次         → 有线，而且是细线，最接近 bigjpg
        #   代价（实测，不是估的）：第二次网络的输入是 4x 图，像素是源图的 16 倍，
        #   所以慢得多 —— 843×1264 的图 4x，anime 23.3s、本档 152.2s（约 6.5 倍）。
        #   真值 PSNR 也让掉约 0.8 dB（26.90 vs 27.71），SSIM 0.8981 vs 0.9083。
        #   一句话：单次网络已经能把线「抬」出来（就是上面那档带通干的事），本档
        #   多花 6 倍时间主要是让线更接近 bigjpg 那种细而闭的轮廓。按需选，别默认开。
        #   第二次网络的输出是原生倍率的平方（anime 是 16x），靠 SRRunner.upscale
        #   的 post_down 边跑边降回目标倍率，所以那层超大图不整幅驻留内存。
        "noise": {0: "4x-AnimeSharp.onnx"},
        "dn_only": None,
        "hint": 4,
        "band": (1.0, 3.0, 0.40),
        "twice": True,
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

# 收尾参数是在 4x 输出上标定的，别的倍率按 scale / SCALE_REF 等比缩放半径。
# 不缩放的话 8x 上线宽翻倍、半径相对只剩一半，收尾基本等于没做 —— 这正是早先
# 8x 成品比自家 4x 还软、比 bigjpg 更软的直接原因（增益拉到 crisp 也一样是空转）。
SCALE_REF = 4

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


def resolve_finish(preset: str, clear: str | float,
                   scale: int = SCALE_REF) -> tuple[str, float, float, float]:
    """收尾 -> (算法, 半径小, 半径大, 增益)。算法是 "band" 或 "unsharp"。

    传数字当 clear 就是直接指定增益。半径一律按 scale / SCALE_REF 缩放。
    """
    p = PRESETS[preset]
    if isinstance(clear, (int, float)):
        mult, forced = 1.0, float(clear)
    else:
        m = CLEAR_LEVELS.get(clear)
        if m is None:
            raise SystemExit(t("err.clear", name=clear, list=", ".join(CLEAR_LEVELS)))
        mult, forced = float(m), None
    k = float(scale) / SCALE_REF
    if "band" in p:
        r1, r2, base = p["band"]
        return "band", r1 * k, r2 * k, (base * mult if forced is None else forced)
    r, base = p.get("sharp", (0.0, 0.0))
    return "unsharp", r * k, 0.0, (base * mult if forced is None else forced)


def plan_runs(preset: str, scale: int, native: int) -> tuple[list[int], int]:
    """这个预设要跑几步网络、每步跑完按几倍降采样。返回 (每步的 post_down, 网络倍率)。

    普通预设就是 plan_passes 的串联次数，每步都不降。
    「优化算法版」额外补一次网络：第二次的原始输出是 native² 倍，让 runner 边跑边降
    （post_down），所以那层超大图从不整幅驻留内存。

    post_down 不能超过网络倍率（降采样是在每块出网络之后做的），所以目标倍率比网络
    倍率还小时，第二次只降到网络倍率，剩下那点差值交给通用的 Lanczos 收尾去补。
    """
    if PRESETS.get(preset, {}).get("twice") and native * native >= scale:
        down = min(native, native * native // scale)
        net = native * native // down
        return [1, down], net
    passes, net_scale = plan_passes(scale, native)
    return [1] * passes, net_scale


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
