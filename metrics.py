# -*- coding: utf-8 -*-
"""
客观指标：PSNR / SSIM / 拉普拉斯方差（锐度）。

bench.py 离线评测和 web/server.py 在线任务都用这一份，避免两套数字对不上。
约定：图像是 float32/float64，取值范围 0..1，形状 (H,W) 或 (H,W,3)。
"""
from __future__ import annotations

import numpy as np

LUMA = np.array([0.299, 0.587, 0.114], np.float64)


def to_luma(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, np.float64)
    return a @ LUMA if a.ndim == 3 else a


def psnr(a: np.ndarray, b: np.ndarray) -> float:
    mse = float(np.mean((np.asarray(a, np.float64) - np.asarray(b, np.float64)) ** 2))
    return 99.0 if mse <= 1e-12 else float(10.0 * np.log10(1.0 / mse))


def _box(x: np.ndarray, r: int) -> np.ndarray:
    """(2r+1)² 均匀窗均值，用积分图算，跟窗口大小无关。"""
    k = 2 * r + 1
    c = np.cumsum(np.cumsum(x, 0), 1)
    c = np.pad(c, ((1, 0), (1, 0)))
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)


def ssim(a: np.ndarray, b: np.ndarray, r: int = 5) -> float:
    """标准 SSIM：11×11 均匀窗，算在亮度通道上。"""
    x, y = to_luma(a), to_luma(b)
    if x.shape != y.shape:
        raise ValueError(f"SSIM 需要同样尺寸，收到 {x.shape} 与 {y.shape}")
    C1, C2 = 0.01 ** 2, 0.03 ** 2
    mx, my = _box(x, r), _box(y, r)
    vx = _box(x * x, r) - mx * mx
    vy = _box(y * y, r) - my * my
    cxy = _box(x * y, r) - mx * my
    num = (2 * mx * my + C1) * (2 * cxy + C2)
    den = (mx ** 2 + my ** 2 + C1) * (vx + vy + C2)
    return float(np.mean(num / den))


def sharpness(a: np.ndarray) -> float:
    """拉普拉斯响应的方差 —— 越大边缘越「立」，用来量化 AI 到底有没有补出细节。"""
    g = to_luma(a).astype(np.float32)
    if min(g.shape) < 3:
        return 0.0
    lap = (4.0 * g[1:-1, 1:-1] - g[:-2, 1:-1] - g[2:, 1:-1]
           - g[1:-1, :-2] - g[1:-1, 2:])
    return float(lap.var())
