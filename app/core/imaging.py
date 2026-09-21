#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""图像读写 + 收尾锐化。"""
from __future__ import annotations

import math
import os

import numpy as np
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True


# --------------------------------------------------------------------------- #
# 收尾锐化（unsharp mask）
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


def unsharp(rgb: np.ndarray, radius: float, gain: float) -> np.ndarray:
    """边缘锐化：rgb + gain * (rgb - 模糊版)。gain<=0 直接原样返回。

    只加在「比模糊版更亮/更暗」的像素上，也就是边缘；平坦区 rgb≈模糊版，
    加了个零 —— 这是它不会把干净区域磨出噪点的原因。
    """
    if gain <= 0 or radius <= 0:
        return rgb
    return np.clip(rgb + gain * (rgb - _gauss_blur(rgb, float(radius))), 0.0, 1.0)


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


