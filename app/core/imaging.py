#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""图像读写 + 收尾细节补偿（零均值带通 / USM）。"""
from __future__ import annotations

import math
import os

import numpy as np
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True


# --------------------------------------------------------------------------- #
# 高斯模糊（收尾带通 / 锐化与源图去碎纹共用）
# --------------------------------------------------------------------------- #
def _gauss_kernel(radius: float) -> np.ndarray:
    k = max(1, int(math.ceil(radius * 3.0)))
    x = np.arange(-k, k + 1, dtype=np.float32)
    w = np.exp(-(x * x) / (2.0 * radius * radius))
    return (w / w.sum()).astype(np.float32)


def _gauss_blur(rgb: np.ndarray, radius: float, row_chunk: int = 256) -> np.ndarray:
    """可分离高斯模糊，float32，边界 reflect。

    按行分块做：16x 的大图有 3 亿多像素，整幅中转一下就是好几个 GB。
    分块后每个中转数组只跟 chunk 行有关，峰值内存跟图高无关。
    """
    w = _gauss_kernel(radius)
    k = len(w) // 2
    H, W, _ = rgb.shape
    out = np.empty_like(rgb)
    for y0 in range(0, H, row_chunk):
        y1 = min(H, y0 + row_chunk)
        y0e, y1e = max(0, y0 - k), min(H, y1 + k)
        blk = rgb[y0e:y1e]
        # 竖向
        pad = np.pad(blk, ((k, k), (0, 0), (0, 0)), mode="reflect")
        v = np.zeros_like(blk)
        for i, wi in enumerate(w):
            v += wi * pad[i:i + blk.shape[0]]
        # 横向
        pad2 = np.pad(v, ((0, 0), (k, k), (0, 0)), mode="reflect")
        o = np.zeros_like(v)
        for i, wi in enumerate(w):
            o += wi * pad2[:, i:i + blk.shape[1]]
        out[y0:y1] = o[y0 - y0e:y1 - y0e]
    return out


# --------------------------------------------------------------------------- #
# 源图去碎纹（进网络之前）
# --------------------------------------------------------------------------- #
# 幅度阈值：压掉多少灰阶以下的高频。55/1000 是拿一张眼珠被网纹糊住的图量出来的 ——
# JPEG 在强边周围留的振铃（蚊噪）只有几个灰阶，真线稿有几十个，切在中间两边都顾得上。
# 高斯半径 0.8（不是 1.0）：网纹在这个尺度上就基本被隔离干净了，而半径收到 1.0 会把
# 稍宽一点的真实细节也卷进来 —— 实测卡通样本 PSNR 从 +2.10 掉到 +1.99，正好跌破门禁。
DECLIP_THRESHOLD = 0.055
DECLIP_SIGMA = 0.8


def declip(rgb: np.ndarray, sigma: float | None = None,
           threshold: float | None = None) -> np.ndarray:
    """把源图里「小幅度的高频」压掉，留下真正的边（JPEG 振铃 / 蚊噪清理）。

    为什么需要这一步：细节模型（4x-AnimeSharp 这类）会把 JPEG 在强边旁边留下的
    几个灰阶的振铃当成纹理，放大成一整片网纹 —— 眼睛、嘴这类「小尺寸 + 四周全是强边」
    的地方最明显，皮肤和雪地这类大块平坦区反而没事。源图越糊、压得越狠，越容易出。

    为什么不能简单模糊：振铃和 1px 线稿在**空间尺度**上是同一个量级，3x3 中值这类
    「按尺度切」的做法会把线稿一起削掉（实测头发细节掉到三分之一）。两者的差别在**幅度**
    —— 振铃几个灰阶、线稿几十个 —— 所以按幅度切：小于 threshold 的高频归零，
    大于 2×threshold 的原样保留。

    中间那段走平滑斜坡，而不是从幅度里减掉一个常数：减去常数会把**所有**高频
    一起削薄，真边也跟着掉 —— 实测卡通样本的 PSNR 会从 +2.67 dB 掉到 +1.07。
    斜坡只动振幅小那一档，大于 2T 的边一个灰阶都不碰。

    只依赖已有的可分离高斯，代价是一遍模糊，跟网络推理比可以忽略。
    """
    # 默认值在这里解析，不写进函数签名 —— 写进签名就固化成定义那一刻的值了，
    # 之后调 DECLIP_THRESHOLD 不生效（扫参数时被这个坑过一次）。
    sigma = DECLIP_SIGMA if sigma is None else float(sigma)
    threshold = DECLIP_THRESHOLD if threshold is None else float(threshold)
    if threshold <= 0 or sigma <= 0:
        return rgb
    base = _gauss_blur(rgb, sigma)
    hf = rgb - base
    w = np.clip((np.abs(hf) - threshold) / threshold, 0.0, 1.0)
    w = w * w * (3.0 - 2.0 * w)          # smoothstep：两端斜率都是 0，不留台阶
    return np.clip(base + hf * w, 0.0, 1.0)


# --------------------------------------------------------------------------- #
# 收尾细节补偿：unsharp mask + 零均值带通
# --------------------------------------------------------------------------- #
def unsharp(rgb: np.ndarray, radius: float, gain: float) -> np.ndarray:
    """边缘锐化：rgb + gain * (rgb - 模糊版)。gain<=0 直接原样返回。

    只加在「比模糊版更亮/更暗」的像素上，也就是边缘；平坦区 rgb≈模糊版，
    加了个零 —— 这是它不会把干净区域磨出噪点的原因。
    """
    if gain <= 0 or radius <= 0:
        return rgb
    return np.clip(rgb + gain * (rgb - _gauss_blur(rgb, float(radius))), 0.0, 1.0)


def detail_band(rgb: np.ndarray, radius_small: float, radius_large: float,
                gain: float) -> np.ndarray:
    """零均值带通补偿：只抬「线宽那一层」尺度，不抬 1px 噪点、不镶光晕。

    和 unsharp 的差别在**高通怎么取**：
      unsharp 取 (原图 - 小半径模糊)。那里面混着 1px 的 JPEG 振铃和噪声，
      抬它的同时会在每条线的两侧各留一道亮边/暗边 —— 也就是光晕。
      带通取 (小半径模糊 - 大半径模糊)，是个零均值的高通，抬的正好是
      radius_small..radius_large 这一层结构，也就是「线本身的宽度」。

    半径按输出倍率缩放，见 presets.resolve_finish()：这些数是在 4x 输出上标定的，
    到 8x 线宽翻倍，半径不跟着翻就等于什么都没做。
    """
    if gain <= 0 or radius_small <= 0 or radius_large <= radius_small:
        return rgb
    band = (_gauss_blur(rgb, float(radius_small))
            - _gauss_blur(rgb, float(radius_large)))
    return np.clip(rgb + gain * band, 0.0, 1.0)


def brighten(rgb: np.ndarray, factor: float) -> np.ndarray:
    """整体乘一个系数：1.0 原样，>1 提亮，<1 压暗。factor<=0 视为原样。

    为什么是「乘」不是「加常数」：量过线上 bigjpg 卡通/插画 4x 的平坦区，
    它在亮部提了 +4.8 灰阶、中间调只提 +1.1 —— 这是乘法（×1.019）的形状，
    加法会把中间调一起顶上去、还让纯黑发灰。三种拟合里乘法残差最小
    （4.11 灰阶，加常数 4.30，提 gamma 5.88）。
    """
    if factor <= 0 or abs(float(factor) - 1.0) < 1e-9:
        return rgb
    return np.clip(rgb * float(factor), 0.0, 1.0)


# --------------------------------------------------------------------------- #
# 图像读写
# --------------------------------------------------------------------------- #
def load_image(path: str) -> tuple[np.ndarray, np.ndarray | None, str]:
    im = Image.open(path)
    src_mode = im.mode
    im = im.convert("RGBA") if src_mode in ("RGBA", "LA", "P") else im.convert("RGB")
    if im.mode == "RGBA":
        arr = np.asarray(im, dtype=np.uint8)
        return (arr[:, :, :3].astype(np.float32) / 255.0,
                arr[:, :, 3].astype(np.float32) / 255.0, src_mode)
    arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
    return arr.astype(np.float32) / 255.0, None, src_mode


def save_image(rgb: np.ndarray, path: str, alpha: np.ndarray | None = None) -> None:
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    arr = (np.clip(rgb, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    if alpha is not None:
        a = (np.clip(alpha, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
        im = Image.fromarray(np.dstack([arr, a]), mode="RGBA")
    else:
        im = Image.fromarray(arr, mode="RGB")
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        im.convert("RGB").save(path, quality=95, subsampling=0, optimize=True)
    elif ext == ".webp":
        im.save(path, quality=95, method=6)
    else:
        im.save(path)


